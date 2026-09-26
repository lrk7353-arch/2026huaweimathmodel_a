"""Experimental P2/P3 graph-to-plan pipeline with one call/time budget.

Candidates are generated from the input graph and this run's successful records.
Stage quotas are engineering defaults, not a claim of optimal budget allocation.
"""
import gzip
import math
from collections import deque

from common_run import *
from advanced_solver.component_baseline import generate_component_candidates
from advanced_solver.operation_assign import generate_operation_candidates
from advanced_solver.trace_refine import generate_trace_candidates
from controller import generate_wcc_candidates
from p23_observed import observed_route, select_wcc_probe


def exact(plan):
    # Insertion order matters to the official scheduler; never sort these keys.
    return json.dumps(plan, ensure_ascii=False, separators=(',', ':'))


def structural_route(ir, cores):
    """Select a search family using only original compute-component work.

    The ratio is a compute-load heuristic: it omits dependencies, COPY, DDR,
    memory and cache. It is not a makespan prediction or a pruning certificate.
    """
    totals = (sum(c.work_m for c in ir.components),
              sum(c.work_v for c in ir.components),
              sum(c.work_other for c in ir.components))
    ideal_pipe_load = max(1., max(totals, default=0) / cores)
    largest_component_pipe_load = max((max(c.work_m, c.work_v, c.work_other)
                                      for c in ir.components), default=0)
    ratio = largest_component_pipe_load / ideal_pipe_load
    route = 'component_wcc' if len(ir.components) > 1 and ratio <= 1.5 else 'staged'
    return dict(route=route, components=len(ir.components),
                ideal_pipe_load=ideal_pipe_load,
                largest_component_pipe_load=largest_component_pipe_load,
                component_to_ideal_ratio=ratio, threshold=1.5,
                basis='original graph only; fixed rule; no case ID, prior score or result lookup')


def run(case, problem, cores, method, out, budget=12, seconds=120, seed=17,
        evaluation_timeout=60, evaluation_dir=None, p3_refinement='legacy', proposal_budget=None,
        operation_policy='legacy'):
    if operation_policy not in ('legacy', 'paired_w200'):
        raise ValueError('unknown operation policy')
    if method in ('integrated','local_legacy','wide_legacy','budget_greedy','budget_beam'):
        if operation_policy != 'legacy':
            raise ValueError('paired operation policy requires trace_routed/staged pipeline')
        if seed!=17 or p3_refinement!='legacy':raise ValueError('integrated pipeline requires seed17 and legacy p3-refinement flag')
        from cold_portfolio import run as integrated_run
        return integrated_run(case,problem,cores,method,out,budget,seconds,evaluation_timeout,evaluation_dir)
    started = time.monotonic()
    if problem not in (2, 3) or cores not in range(1, 6):
        raise ValueError('P2/P3 and 1..5 cores required')
    if method not in ('component_wcc', 'staged', 'routed', 'trace_routed'):
        raise ValueError('method must be component_wcc, staged, routed or trace_routed')
    if type(budget) is not int or budget < 1 or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('positive integer budget and finite positive seconds required')
    proposal_budget=budget if proposal_budget is None else proposal_budget
    if type(proposal_budget) is not int or proposal_budget<1:raise ValueError('positive proposal budget required')
    if not math.isfinite(evaluation_timeout) or evaluation_timeout <= 0:
        raise ValueError('finite positive evaluation_timeout required')
    if seed != 17:
        raise ValueError('This experimental pipeline currently requires seed 17')
    if p3_refinement not in ('legacy', 'feedback'):
        raise ValueError('P3 from-scratch refinement must be legacy or feedback')
    if problem == 3 and method != 'component_wcc' and p3_refinement == 'feedback' and evaluation_timeout != 60:
        raise ValueError('feedback currently requires evaluation_timeout=60')
    out = Path(out).resolve()
    if out == DATA or DATA in out.parents:
        raise ValueError('Outputs must be outside official data')
    out.mkdir(parents=True, exist_ok=False)
    evdir = Path(evaluation_dir) if evaluation_dir is not None else R / 'advanced_solver/runs/formal_v2/evaluations'
    deadline = started + seconds
    ir = GraphIR.from_path(DATA / (case + '.json'))
    routing = structural_route(ir, cores) if method in ('routed', 'trace_routed') else None
    effective_method = routing['route'] if routing else method
    calls, skipped, stages = [], [], []
    seen, best, best_component = set(), None, None
    pools = {name: deque() for name in ('component', 'wcc', 'operation')}
    wcc_parent = None

    def available():
        return len(calls) < budget and time.monotonic() < deadline

    def checkpoint():
        atomic_json(out / 'progress.json', dict(case=case, problem=problem, num_cores=cores,
                    method=method, logical_calls=len(calls), budget=budget,
                    best_record=best, elapsed_seconds=time.monotonic() - started))

    def apply(candidate, phase):
        nonlocal best, best_component
        if not available():
            return False
        plan = candidate['plan']
        signature = exact(plan)
        if signature in seen:
            skipped.append(dict(name=candidate['name'], phase=phase, reason='exact_ordered_duplicate'))
            return False
        try:
            validate_plan(ir, plan)
            if len(plan['core_schedules']) != cores:
                raise ValueError('candidate core count mismatch')
        except (ValueError, KeyError, TypeError) as exc:
            seen.add(signature)
            skipped.append(dict(name=candidate['name'], phase=phase, reason='invalid_plan', error=str(exc)))
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0 or len(calls) >= budget:
            return False
        seen.add(signature)
        # Reserve the logical call before invoking the evaluator. Failure,
        # wrapper exceptions and successful cache hits all consume this call.
        trial = dict(name=candidate['name'], phase=phase,
                     metadata=candidate.get('metadata', {}), accepted=False)
        calls.append(trial)
        try:
            record = evaluate(DATA / (case + '.json'), plan, problem, evdir,
                              timeout=min(evaluation_timeout, remaining), config_path=DATA / 'config.txt')
        except Exception as exc:
            record = dict(status='wrapper_exception', cache_hit=False,
                          error=repr(exc), graph_path=str(DATA / (case + '.json')), problem=problem)
        trial['record'] = record
        if record['status'] == 'success':
            if phase == 'component' and (best_component is None or score(record) < score(best_component)):
                best_component = record
            if best is None or score(record) < score(best):
                best = record
                trial['accepted'] = True
                atomic_json(out / 'best.plan.json', plan)
        checkpoint()
        return True

    def generate(phase, function, *args, **kwargs):
        if not available():
            return []
        before = time.monotonic()
        try:
            candidates, diagnostics = function(*args, **kwargs)
            stages.append(dict(phase=phase, generation_seconds=time.monotonic() - before,
                               candidate_count=len(candidates), diagnostics=diagnostics))
            return candidates
        except Exception as exc:
            stages.append(dict(phase=phase, generation_seconds=time.monotonic() - before,
                               candidate_count=0, error=repr(exc)))
            return []

    def consume(pool, cap, phase):
        limit = min(budget, len(calls) + max(0, cap))
        while pool and available() and len(calls) < limit:
            apply(pool.popleft(), phase)

    def refresh_wcc():
        nonlocal wcc_parent
        if best_component is None or not available():
            return
        plan = read_json(best_component['plan_path'])
        signature = exact(plan)
        if signature == wcc_parent:
            return
        wcc_parent = signature
        # An operation assignment may split WCCs across cores. Always retain
        # the component incumbent as this generator's independent parent.
        pools['wcc'].extend(generate('wcc', generate_wcc_candidates, ir, plan,
            num_cores=cores, max_candidates=max(2, proposal_budget), seed=seed, policy='mixed'))

    def structure_fallback(include_operation):
        families = ('component', 'wcc', 'operation') if include_operation else ('component', 'wcc')
        while available():
            refresh_wcc()
            if not any(pools[name] for name in families):
                break
            for phase in families:
                consume(pools[phase], 1, phase)
                if phase == 'component':
                    refresh_wcc()

    checkpoint()
    pools['component'].extend(generate('component', generate_component_candidates, ir, cores,
                                       max_candidates=max(4, proposal_budget), seed=seed))
    consume(pools['component'], 2, 'component')
    refresh_wcc()
    consume(pools['wcc'], 2, 'wcc')

    if method == 'trace_routed' and available():
        structural = routing
        try:
            routing = observed_route(ir, best, cores, structural)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            routing = dict(structural=structural, route='staged', probe=False,
                           reason='unusable_timeline', error=str(exc))
        effective_method = routing['route']
        routing['decision_after_calls'] = len(calls)
        if routing.get('probe') and best is not None:
            candidate, details = select_wcc_probe(pools['wcc'], seen, exact,
                                                  best['metrics'].get('capacity_bytes', {}))
            routing['probe_selection'] = details
            if candidate is not None:
                # Remove only the chosen candidate, preserving all other order.
                pools['wcc'] = deque(c for c in pools['wcc'] if c is not candidate)
                apply(candidate, 'wcc_probe')

    if effective_method == 'component_wcc':
        structure_fallback(False)
    else:
        # Operation proposals do not depend on an incumbent. Even if every
        # Component trial failed, they still have a chance to find feasibility.
        operation_candidates = generate('operation', generate_operation_candidates, ir, cores,
                                        max_candidates=max(4, proposal_budget), seed=seed)
        pools['operation'].extend(operation_candidates)
        consume(pools['operation'], 2, 'operation')
        if operation_policy == 'paired_w200':
            # Ownership and order form one complete candidate. Do not require
            # the unrefined ownership to win an official call before repairing
            # its ordering; that acceptance barrier hid strong stable-w200 layouts.
            # The two established operation controls remain first. Every new
            # evaluation consumes this pipeline's original shared budget.
            from event_frontier import candidates as insertion_candidates
            for wanted in ('op_stable_id_w200', 'op_critical_path_w200'):
                if not available():
                    break
                source = next((c for c in operation_candidates
                               if wanted == c['name'] or wanted in c.get('metadata', {}).get('aliases', [])), None)
                if source is None:
                    continue
                t = time.monotonic()
                try:
                    c = next(insertion_candidates(ir, problem, cores, source['plan'], deadline))
                    c = dict(c, name=wanted+'_fixed_insertion', metadata=dict(
                        c['metadata'], source_family=source['metadata'], operation_policy=operation_policy))
                    stages.append(dict(phase='paired_operation_generation', name=c['name'],
                                       generation_seconds=time.monotonic()-t))
                    apply(c, 'paired_operation')
                except (StopIteration, TimeoutError, ValueError) as exc:
                    stages.append(dict(phase='paired_operation_generation', name=wanted,
                                       generation_seconds=time.monotonic()-t, error=repr(exc)))
        trace_limit = min(budget, len(calls) + 2) if problem == 3 else budget
        for round_index in range(3):
            if not available() or best is None or len(calls) >= trace_limit:
                break
            with gzip.open(best['result_path'], 'rt') as handle:
                raw = json.load(handle)
            candidates = generate('trace', generate_trace_candidates, ir, read_json(best['plan_path']), raw,
                                  num_cores=cores, max_candidates=trace_limit - len(calls),
                                  round_index=round_index, seed=seed)
            consume(deque(candidates), trace_limit - len(calls), 'trace')
        if problem == 3 and best is not None and available():
            if p3_refinement == 'legacy':
                from run_p3_refine import run as cache_run
                child_args = (case, best, None, out / 'cache')
                child_kwargs = dict(per_call_timeout=evaluation_timeout)
            else:
                from p3_feedback import run as cache_run
                child_args = (case, best, out / 'cache')
                child_kwargs = dict(policy='feedback', seed=seed)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                child = cache_run(*child_args, budget=budget - len(calls), seconds=remaining,
                                  cores=cores, evaluation_dir=evdir, seen_signatures=seen,
                                  **child_kwargs)
                if child['logical_calls'] != len(child['calls']) or len(calls) + len(child['calls']) > budget:
                    raise AssertionError('child call accounting exceeds the shared budget')
                calls.extend(dict(trial, phase='p3_cache', cache_refinement=p3_refinement)
                             for trial in child['calls'])
                for trial in child['calls']:
                    plan_path = trial['record'].get('plan_path')
                    if plan_path:
                        seen.add(exact(read_json(plan_path)))
                best = child['best_record']
                stages.append(dict(phase='p3_cache', method=p3_refinement,
                                   logical_calls=child['logical_calls'], stop_reason=child['stop_reason']))
                atomic_json(out / 'best.plan.json', read_json(best['plan_path']))
                checkpoint()
        # Empty neighbourhoods do not burn their reserved quota. Any remaining
        # calls go back to untried structural candidates, still under one cap.
        structure_fallback(True)

    stopped = 'time_budget' if time.monotonic() >= deadline else ('call_budget' if len(calls) >= budget else 'candidate_pools_exhausted')
    result = dict(case=case, problem=problem, num_cores=cores, method=method, seed=seed,
                  effective_method=effective_method, routing=routing,
                  status='success' if best is not None else 'no_feasible_result', best_record=best,
                  best_component=best_component, calls=calls, logical_calls=len(calls),
                  new_calls=sum(not trial['record'].get('cache_hit', False) for trial in calls),
                  elapsed_seconds=time.monotonic() - started, stop_reason=stopped,
                  budget=budget, proposal_budget=proposal_budget, soft_time_budget=seconds, evaluation_timeout=evaluation_timeout,
                  evaluation_dir=str(evdir), p3_refinement=p3_refinement, operation_policy=operation_policy,
                  skipped=skipped, stages=stages,
                  scope='Experimental from-scratch P2/P3 pipeline; all initial/failure/cache-hit calls charged; '
                        'shared soft deadline includes graph loading and candidate generation; no historical incumbents; '
                        'fixed stage quotas are engineering defaults, not an optimality claim.')
    if best is not None:
        atomic_json(out / 'best.plan.json', read_json(best['plan_path']))
    atomic_json(out / 'summary.json', result)
    return result
