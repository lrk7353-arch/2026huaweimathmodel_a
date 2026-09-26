"""Budgeted CP-SAT backend for a *fixed* common P1 block partition.

Only active block ownership and all compatible core orders are decision
variables.  Block membership is supplied by the caller, not optimized here.
Outside blocks keep their core and the relative-order edges in ``preds``.
The shared proxy omits dynamic DDR contention/spills: even an OPTIMAL result
is optimal only for this restricted proxy, never for the official evaluator.

Time is represented in 1/60 cycle ticks.  This exactly represents the shared
``integer compute + integer bytes / 60`` duration formula.  Other positive
durations are conservatively rounded upward and the error is reported.
"""
import copy
import heapq
import math
import time

from common_run import validate_plan


SCALE = 60
SAME_CORE_WAIT = 100
CROSS_CORE_WAIT = 1000
MAX_SECONDS = 20.0
SCOPE = ('fixed common block partition; joint active-block core assignment '
         'and feasible order; outside core and relative order retained; '
         'approximate duration/communication objective, not official optimality')


def _check(deadline):
    if time.perf_counter() >= deadline:
        raise TimeoutError('CP-SAT construction deadline')


def _inputs(model):
    blocks = model['blocks']
    size = len(blocks)
    ncores = model['ncores']
    if type(ncores) is not int or ncores < 1:
        raise ValueError('ncores must be a positive integer')
    preds = [set(p) for p in model['preds']]
    durations = [float(d) for d in model['durations']]
    if len(preds) != size or len(durations) != size:
        raise ValueError('block, predecessor and duration lengths differ')
    active = list(model['active'])
    fixed = dict(model['fixed'])
    if (len(set(active)) != len(active) or set(active) & set(fixed)
            or set(active) | set(fixed) != set(range(size))):
        raise ValueError('active and fixed must partition all block indices')
    if any(type(b) is not int for b in active) or any(type(b) is not int for b in fixed):
        raise ValueError('block indices must be integers')
    if any(type(c) is not int or not 0 <= c < ncores for c in fixed.values()):
        raise ValueError('fixed owner outside core range')
    if any(not math.isfinite(d) or d <= 0 for d in durations):
        raise ValueError('durations must be positive and finite')
    successors = [[] for _ in blocks]
    indegree = [len(p) for p in preds]
    for block, parents in enumerate(preds):
        for parent in parents:
            if type(parent) is not int or not 0 <= parent < size or parent == block:
                raise ValueError('invalid predecessor index')
            successors[parent].append(block)
    ready = [b for b in range(size) if not indegree[b]]
    heapq.heapify(ready)
    topo = []
    while ready:
        block = heapq.heappop(ready)
        topo.append(block)
        for child in successors[block]:
            indegree[child] -= 1
            if not indegree[child]:
                heapq.heappush(ready, child)
    if len(topo) != size:
        raise ValueError('predecessors contain a cycle, including outside order')
    # Remove only numerical round-off around an exact integer tick.  Otherwise
    # ceil, rather than nearest rounding, prevents downward duration estimates.
    ticks = []
    for value in durations:
        scaled = value * SCALE
        nearest = round(scaled)
        ticks.append(max(1, int(nearest if abs(scaled-nearest) <= 1e-7
                                else math.ceil(scaled))))
    return blocks, preds, durations, ticks, active, fixed, ncores, topo


def _warm_schedule(model, preds, durations, fixed, ncores, topo):
    """Feasible earliest schedule, without fixing any observed absolute time."""
    preferred = model.get('preferred_owner', {})
    owners, starts, ends = {}, {}, {}
    available = [0] * ncores
    for block in topo:
        try:
            default = preferred[block]
        except (KeyError, IndexError, TypeError):
            default = 0
        core = fixed.get(block, default)
        if type(core) is not int or not 0 <= core < ncores:
            core = fixed.get(block, 0)
        release = max((ends[p] + (CROSS_CORE_WAIT*SCALE
                                  if owners[p] != core else 0)
                       for p in preds[block]), default=0)
        start = max(available[core], release)
        owners[block], starts[block] = core, start
        ends[block] = start + durations[block]
        available[core] = ends[block] + SAME_CORE_WAIT*SCALE
    return owners, starts, ends


def _plan(model, owners, starts):
    """Retain incumbent op-key insertion order, which can affect compilation."""
    mapping = {str(op): b for b, block in enumerate(model['blocks']) for op in block}
    original = model.get('incumbent', {}).get('node_to_subgraph', {})
    ordered = {op: mapping[op] for op in original if op in mapping}
    ordered.update({op: b for op, b in mapping.items() if op not in ordered})
    schedules = [[] for _ in range(model['ncores'])]
    for block in sorted(owners, key=lambda b: (starts[b], b)):
        schedules[owners[block]].append(block)
    return {'node_to_subgraph': ordered, 'core_schedules': schedules}


def _checked_warm_plan(ir, model, warm_plan, preds, ticks, fixed, ncores):
    """Validate a same-partition warm plan and recompute its earliest proxy.

    Original graph validation alone does not validate the added outside-order
    edges in the common model.  Their union with warm core sequences is checked
    explicitly before any warm schedule is used as a bound or fallback.
    """
    validate_plan(ir, warm_plan)
    if len(warm_plan['core_schedules']) != ncores:
        raise ValueError('warm plan core count differs from model owner domain')
    expected = {str(op): b for b, block in enumerate(model['blocks']) for op in block}
    if warm_plan['node_to_subgraph'] != expected:
        raise ValueError('warm plan must use the exact common model partition and block IDs')
    owners = {b: c for c, seq in enumerate(warm_plan['core_schedules']) for b in seq}
    if any(owners[b] != c for b, c in fixed.items()):
        raise ValueError('warm plan changes a fixed outside core')
    previous = {b: a for seq in warm_plan['core_schedules'] for a, b in zip(seq, seq[1:])}
    combined = [set(parents) for parents in preds]
    for block, parent in previous.items():
        combined[block].add(parent)
    successors = [[] for _ in combined]
    degree = list(map(len, combined))
    for block, parents in enumerate(combined):
        for parent in parents:
            successors[parent].append(block)
    ready = [b for b in range(len(combined)) if not degree[b]]
    heapq.heapify(ready)
    starts, ends = {}, {}
    while ready:
        block = heapq.heappop(ready)
        release = max((ends[p] + (CROSS_CORE_WAIT*SCALE if owners[p] != owners[block] else 0)
                       for p in preds[block]), default=0)
        available = ends[previous[block]]+SAME_CORE_WAIT*SCALE if block in previous else 0
        starts[block] = max(release, available)
        ends[block] = starts[block]+ticks[block]
        for child in successors[block]:
            degree[child] -= 1
            if not degree[child]:
                heapq.heappush(ready, child)
    if len(ends) != len(combined):
        raise ValueError('warm core order conflicts with model predecessors/outside order')
    return copy.deepcopy(warm_plan), owners, starts, ends


def solve_cpsat(ir, model, deadline=float('inf'), seed=17, warm_plan=None):
    """Return ``(legal_plan_or_None, diagnostics)`` within the supplied budget.

    ``deadline`` is an absolute ``time.perf_counter()`` timestamp.  It includes
    construction and extraction; this function additionally caps its own work
    at 20 seconds.  The caller must include its model preparation in its budget.
    OR-Tools should be pre-imported once for both benchmark arms before timing.
    Supplying ``warm_plan`` enables a beam-to-CP hybrid: the plan must have the
    exact common partition, is validated and rescored, is fully hinted, and
    supplies a makespan upper bound.  Only a strict rounded-proxy improvement
    replaces it; otherwise the verified warm plan is returned.  This guarantee
    concerns the proxy, not the official execution time.  Without ``warm_plan``
    the original CP-only behavior is retained. No official simulator is called.
    """
    started = time.perf_counter()
    deadline = min(float(deadline), started + MAX_SECONDS)
    diag = {'backend': 'cp_sat_warm' if warm_plan is not None else 'cp_sat',
            'hybrid': warm_plan is not None, 'scope': SCOPE, 'seed': int(seed),
            'num_search_workers': 1, 'time_scale': SCALE,
            'same_core_wait': SAME_CORE_WAIT, 'cross_core_wait': CROSS_CORE_WAIT,
            'optimality_scope': 'restricted fixed-partition rounded proxy only'}
    checked_warm = None

    def return_warm(reason):
        plan, warm_owner, warm_start, warm_end = checked_warm
        objective = max(warm_end.values(), default=0)
        diag.update(returned='verified_warm_fallback', warm_fallback=True,
                    fallback_reason=reason, objective_ticks=objective,
                    proxy_makespan=objective/SCALE, proxy_gain_cycles=0.0,
                    starts_cycles=[warm_start[b]/SCALE for b in range(len(model['blocks']))],
                    owners=[warm_owner[b] for b in range(len(model['blocks']))],
                    elapsed_seconds=time.perf_counter()-started)
        return plan, diag

    try:
        _check(deadline)
        from ortools.sat.python import cp_model
        blocks, preds, durations, ticks, active, fixed, ncores, topo = _inputs(model)
        if warm_plan is not None:
            checked_warm = _checked_warm_plan(ir, model, warm_plan, preds, ticks, fixed, ncores)
            warm_objective = max(checked_warm[3].values(), default=0)
            diag.update(warm_validated=True, warm_objective_ticks=warm_objective,
                        warm_proxy_makespan=warm_objective/SCALE,
                        warm_upper_bound_ticks=warm_objective,
                        warm_guarantee_scope='rounded model proxy only; no official timing guarantee')
        _check(deadline)
        diag.update(blocks=len(blocks), active_blocks=len(active),
                    fixed_blocks=len(fixed), cores=ncores,
                    max_duration_rounding_cycles=max(
                        (max(0.0, t/SCALE-d) for t, d in zip(ticks, durations)), default=0.0))
        if not blocks:
            plan = _plan(model, {}, {})
            validate_plan(ir, plan)
            diag.update(status='OPTIMAL', proxy_makespan=0.0, objective_ticks=0,
                        best_bound_ticks=0, elapsed_seconds=time.perf_counter()-started)
            return plan, diag
        horizon = sum(ticks) + len(blocks) * CROSS_CORE_WAIT * SCALE
        if horizon >= 2**61:
            raise ValueError('scaled scheduling horizon exceeds safe integer range')
        cp = cp_model.CpModel()
        start, end, padded, owner, present, differences = {}, {}, {}, {}, {}, {}
        intervals = [[] for _ in range(ncores)]
        for block in range(len(blocks)):
            if block % 64 == 0:
                _check(deadline)
            start[block] = cp.new_int_var(0, horizon, f's_{block}')
            end[block] = cp.new_int_var(0, horizon, f'e_{block}')
            padded[block] = cp.new_int_var(0, horizon+SAME_CORE_WAIT*SCALE, f'p_{block}')
            cp.add(end[block] == start[block]+ticks[block])
            cp.add(padded[block] == end[block]+SAME_CORE_WAIT*SCALE)
            length = ticks[block]+SAME_CORE_WAIT*SCALE
            if block in fixed:
                core = fixed[block]
                owner[block] = cp.new_constant(core)
                intervals[core].append(cp.new_interval_var(
                    start[block], length, padded[block], f'i_{block}_{core}'))
            else:
                owner[block] = cp.new_int_var(0, ncores-1, f'c_{block}')
                present[block] = []
                for core in range(ncores):
                    chosen = cp.new_bool_var(f'x_{block}_{core}')
                    present[block].append(chosen)
                    cp.add(owner[block] == core).only_enforce_if(chosen)
                    intervals[core].append(cp.new_optional_interval_var(
                        start[block], length, padded[block], chosen, f'i_{block}_{core}'))
                cp.add_exactly_one(present[block])
        for group in intervals:
            cp.add_no_overlap(group)
        for block, parents in enumerate(preds):
            if block % 64 == 0:
                _check(deadline)
            for parent in parents:
                if parent in fixed and block in fixed:
                    delay = CROSS_CORE_WAIT*SCALE if fixed[parent] != fixed[block] else 0
                    cp.add(start[block] >= end[parent]+delay)
                else:
                    different = cp.new_bool_var(f'diff_{parent}_{block}')
                    differences[parent, block] = different
                    cp.add(owner[parent] != owner[block]).only_enforce_if(different)
                    cp.add(owner[parent] == owner[block]).only_enforce_if(different.Not())
                    cp.add(start[block] >= end[parent]+CROSS_CORE_WAIT*SCALE*different)
        makespan = cp.new_int_var(0, horizon, 'makespan')
        cp.add_max_equality(makespan, list(end.values()))
        cp.minimize(makespan)
        if checked_warm is None:
            warm_owner, warm_start, warm_end = _warm_schedule(model, preds, ticks, fixed, ncores, topo)
        else:
            _, warm_owner, warm_start, warm_end = checked_warm
            cp.add(makespan <= warm_objective)
        for block in range(len(blocks)):
            cp.add_hint(start[block], warm_start[block])
            cp.add_hint(end[block], warm_end[block])
            cp.add_hint(padded[block], warm_end[block]+SAME_CORE_WAIT*SCALE)
            if block in present:
                cp.add_hint(owner[block], warm_owner[block])
                for core, chosen in enumerate(present[block]):
                    cp.add_hint(chosen, int(warm_owner[block] == core))
        if checked_warm is not None:
            for (parent, block), different in differences.items():
                cp.add_hint(different, int(warm_owner[parent] != warm_owner[block]))
            # Fixed owners share cached constant variables.  Hint each once so
            # the complete hint contains no duplicate variable indices.
            constants = {owner[b].index: (owner[b], fixed[b]) for b in fixed}
            for constant, value in constants.values():
                cp.add_hint(constant, value)
        cp.add_hint(makespan, max(warm_end.values()))
        _check(deadline)
        diag['construction_seconds'] = time.perf_counter()-started
        solver = cp_model.CpSolver()
        # Retain a small extraction allowance.  The caller may reserve additional
        # time for common proxy recomputation and serialization outside this API.
        solver.parameters.max_time_in_seconds = max(0.001, deadline-time.perf_counter()-0.1)
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = int(seed)
        solver.parameters.log_search_progress = False
        result = solver.solve(cp)
        diag.update(status=solver.status_name(result),
                    solve_seconds=solver.wall_time,
                    conflicts=solver.num_conflicts, branches=solver.num_branches)
        if result not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            if checked_warm is not None:
                return return_warm('no_feasible_cp_solution')
            diag['elapsed_seconds'] = time.perf_counter()-started
            return None, diag
        objective = int(round(solver.objective_value))
        if checked_warm is not None:
            diag.update(solver_objective_ticks=objective,
                        best_bound_ticks=float(solver.best_objective_bound))
            if objective >= warm_objective:
                return return_warm('no_strict_proxy_gain')
        actual_owner = {b: int(solver.value(owner[b])) for b in range(len(blocks))}
        actual_start = {b: int(solver.value(start[b])) for b in range(len(blocks))}
        plan = _plan(model, actual_owner, actual_start)
        validate_plan(ir, plan)
        diag.update(objective_ticks=objective,
                    best_bound_ticks=float(solver.best_objective_bound),
                    proxy_makespan=float(solver.objective_value)/SCALE,
                    starts_cycles=[actual_start[b]/SCALE for b in range(len(blocks))],
                    owners=[actual_owner[b] for b in range(len(blocks))],
                    elapsed_seconds=time.perf_counter()-started)
        if checked_warm is not None:
            diag.update(returned='cp_proxy_improvement', warm_fallback=False,
                        proxy_gain_cycles=(warm_objective-objective)/SCALE)
        return plan, diag
    except TimeoutError as exc:
        diag.update(status='CONSTRUCTION_TIMEOUT', error=str(exc),
                    elapsed_seconds=time.perf_counter()-started)
        if checked_warm is not None:
            return return_warm('construction_deadline_after_warm_validation')
        return None, diag
