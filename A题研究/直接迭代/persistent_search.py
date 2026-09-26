"""Cold, paid, bounded continuous search for all three official scenarios.

Two arms share the same graph-only initializer: the previous near-best beam
with bulk local queues, and persistent structural lineages with complete joint
moves. Neither arm reads a historical library. All evaluations, including
failures and cache hits, consume the one shared call budget.
"""
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
import argparse
import gzip
import json
import math
from pathlib import Path
import signal
import time

from common_run import DATA, GraphIR, atomic_json, evaluate, read_json, score, validate_plan
from persistent_budget import PersistentBudget, exact_signature, structural_signature
from persistent_seeds import factories


@contextmanager
def generation_limit(seconds):
    """Linux main-thread hard limit; evaluator subprocesses run outside it."""
    if seconds <= 0:
        raise TimeoutError('no remaining generation time')
    if not hasattr(signal, 'setitimer'):
        raise RuntimeError('bounded search requires POSIX interval timers (use WSL/Linux)')
    def alarm(_signum, _frame):
        raise TimeoutError('candidate generation/observation deadline')
    previous = signal.signal(signal.SIGALRM, alarm)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.setitimer(signal.ITIMER_REAL, min(seconds, old_timer[0]) if old_timer[0]>0 else seconds)
    started = time.monotonic()
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if old_timer[0] > 0:
            remaining = old_timer[0]-(time.monotonic()-started)
            if remaining <= 0:
                raise TimeoutError('outer generation deadline exhausted')
            signal.setitimer(signal.ITIMER_REAL, remaining, old_timer[1])


@dataclass
class Frame:
    record: dict
    depth: int
    plan: dict = None
    raw: dict = None
    observation: dict = None
    observation_attempted: bool = False
    streams: dict = field(default_factory=dict)
    exhausted: set = field(default_factory=set)
    cursor: int = 0
    stale_lease: int = 0
    calls: int = 0
    joint_round: int = 0
    resume_family: str = None
    last_family: str = None

    def protect_queue(self, family):
        """Grant one paid continuation of this exact parent/family iterator."""
        self.stale_lease = max(self.stale_lease, 1)
        self.resume_family = family

    def next_family(self, order):
        if self.resume_family is not None:
            if self.resume_family not in self.exhausted:
                return self.resume_family
            self.resume_family = None
        family = order[self.cursor % len(order)]
        self.cursor += 1
        return family

    def served(self, family):
        self.calls += 1
        self.last_family = family
        if self.resume_family == family:
            self.resume_family = None


def run(case, problem, cores, out, budget=24, seconds=240, timeout=60, variant='persistent'):
    if variant not in ('persistent', 'legacy'):
        raise ValueError('variant must be persistent or legacy')
    if type(problem) is not int or problem not in (1, 2, 3) or type(cores) is not int or cores not in range(1, 6):
        raise ValueError('invalid problem/core count')
    if type(budget) is not int or budget < 1 or not all(math.isfinite(x) and x > 0 for x in (seconds, timeout)):
        raise ValueError('positive finite budgets required')
    out = Path(out).resolve()
    if out == DATA or DATA in out.parents:
        raise ValueError('output must be outside official data')
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    deadline = started + seconds
    ir = GraphIR.from_path(DATA/(case+'.json'))
    calls, generations, skips, branch_events, records = [], [], [], [], []
    seen, best = set(), None
    generation_seconds = 0.
    generation_depth = 0
    allocation = None
    # Reserve a useful continuation window even after a giant seed times out.
    # This is the same explicit rule in both arms, not an unreported extension.
    initialization_deadline = min(deadline, started + min(100., .5*seconds))
    initialization_cap = min(6, budget)
    initial_hashes = []
    initialization_calls = 0
    params = dict(case=case, problem=problem, cores=cores, variant=variant,
        budget=budget, seconds=seconds, timeout=timeout, seed=17,
        initialization_cap=initialization_cap,
        initialization_seconds=min(100., .5*seconds), generation_step_seconds=12,
        branch_width=3, protected_calls_per_lineage=2,
        scope='from original graph; current-run paid records only; unmodified official evaluator')
    atomic_json(out/'input.json', params)

    def available():
        return len(calls) < budget and time.monotonic() < deadline

    def checkpoint(complete=False):
        status = 'success' if best else 'no_feasible_result'
        result = dict(**params, num_cores=cores, method='persistent_'+variant,
            status=status, complete=complete, logical_calls=len(calls),
            calls=calls, best_record=best, initialization_calls=initialization_calls,
            initialization_plan_hashes=initial_hashes,
            initialization_records=[c['record']['record_path'] for c in calls
                if c['phase']=='initialization' and c['record'].get('status')=='success'],
            generation_seconds=generation_seconds, generations=generations,
            skipped=skips, branch_events=branch_events,
            elapsed_seconds=time.monotonic()-started,
            new_calls=sum(not c['record'].get('cache_hit', False) for c in calls),
            failed_calls=sum(c['record'].get('status')!='success' for c in calls),
            stop_reason=('time_budget' if time.monotonic()>=deadline else
                         'call_budget' if len(calls)>=budget else 'candidate_pools_exhausted') if complete else None)
        if allocation is not None:
            result['allocation'] = allocation.summary()
        if records:
            span = score(best)[0]
            result['tradeoffs'] = {str(tolerance): min(
                (r for r in records if score(r)[0] <= (1+tolerance)*span),
                key=lambda r: (score(r)[1], score(r)[0])) for tolerance in (0., .01, .03)}
        atomic_json(out/('summary.json' if complete else 'progress.json'), result)
        return result

    def generate(name, function, parent=None, limit=12):
        nonlocal generation_seconds, generation_depth
        begin = time.monotonic()
        outermost = generation_depth == 0
        generation_depth += 1
        status, error, value, diagnostics = 'success', None, None, None
        try:
            with generation_limit(min(limit, deadline-begin)):
                value = function()
        except StopIteration as exc:
            status = 'exhausted'
            diagnostics = exc.value
        except TimeoutError as exc:
            status, error = 'generation_timeout', str(exc)
        except Exception as exc:
            status, error = 'generation_error', repr(exc)
        elapsed = time.monotonic()-begin
        generation_depth -= 1
        if outermost:
            generation_seconds += elapsed
        generations.append(dict(name=name, parent_record=parent,
            status=status, elapsed_seconds=elapsed, included_in_parent_generation=not outermost,
            error=error, diagnostics=diagnostics))
        return value, status

    def apply(candidate, phase, parent=None, lineage=None, depth=0, call_deadline=None,
              parent_makespan=None):
        nonlocal best
        plan = candidate['plan']
        sig = exact_signature(plan)
        if sig in seen:
            skips.append(dict(name=candidate['name'], reason='exact_ordered_duplicate', phase=phase))
            return None
        try:
            validate_plan(ir, plan)
            if len(plan['core_schedules']) != cores:
                raise ValueError('wrong number of cores')
        except Exception as exc:
            skips.append(dict(name=candidate['name'], reason='invalid_candidate', phase=phase, error=repr(exc)))
            return None
        # Only a mathematically necessary bound for this fixed P1 candidate can
        # prune it. Approximate finishes/copy estimates never hard-prune plans.
        bound = None
        if problem == 1 and best and phase != 'initialization':
            def fixed_bound():
                from p1_selective import task_lower_bound
                from p1_boundary_lower_bound import boundary_ddr_lower_bound
                return max(task_lower_bound(ir, plan)['value'], boundary_ddr_lower_bound(ir, plan)['lower_bound'])
            bound, _ = generate('fixed_candidate_bound', fixed_bound, parent)
            ceiling = parent_makespan if variant=='persistent' and parent_makespan is not None else score(best)[0]
            if bound is not None and bound > ceiling:
                skips.append(dict(name=candidate['name'], phase=phase,
                                  reason='fixed_candidate_necessary_bound', lower_bound=bound,
                                  pruning_ceiling=ceiling, parent_record=parent))
                return None
        remaining = min(deadline, call_deadline or deadline)-time.monotonic()
        if not available() or remaining <= 0:
            return None
        trial = dict(name=candidate['name'], phase=phase, metadata=candidate.get('metadata', {}),
            parent_record=parent, lineage=lineage, parent_depth=depth, lower_bound=bound,
            plan_signature=sig, accepted=False, local_accepted=False)
        # A parent-relative bound rejection is not an evaluated plan. Another
        # slower parent may still improve with it; dedup only charged attempts.
        seen.add(sig)
        calls.append(trial)  # reserve before every possible official invocation
        try:
            r = evaluate(ir.path, plan, problem, out/'evaluations',
                         timeout=min(timeout, remaining), config_path=DATA/'config.txt')
        except Exception as exc:
            r = dict(status='wrapper_exception', error=repr(exc), cache_hit=False,
                     graph_path=str(ir.path), problem=problem)
        trial['record'] = r
        if r['status'] == 'success':
            if bound is not None and bound > score(r)[0]:
                raise AssertionError('candidate lower bound exceeded official result')
            records.append(r)
            if best is None or score(r) < score(best):
                best = r
                trial['accepted'] = True
                atomic_json(out/'best.plan.json', plan)
        trial['elapsed_seconds'] = time.monotonic()-started
        trial['best_makespan'] = score(best)[0] if best else None
        checkpoint()
        return trial

    checkpoint()
    seed_records = []
    for name, factory in factories(ir, problem, cores):
        if len(calls) >= initialization_cap or time.monotonic() >= initialization_deadline:
            break
        candidate, status = generate('initialize_'+name, factory,
                                     limit=min(12., initialization_deadline-time.monotonic()))
        if candidate is None:
            continue
        trial = apply(candidate, 'initialization', call_deadline=initialization_deadline)
        if trial is not None:
            initialization_calls += 1
            initial_hashes.append(trial['plan_signature'])
            if trial['record']['status'] == 'success':
                seed_records.append((trial['record'], candidate['plan'], name))
    checkpoint()

    if not best or not available():
        return checkpoint(True)

    if variant == 'legacy':
        # Reuse the previous beam lifecycle and candidate implementation with
        # this identical initializer. This is the declared loop ablation, not
        # a claim to be byte-identical to the released whole solver.
        from search_budget import SearchBudget
        from cold_portfolio import local_order
        from refine_regions import candidates
        order, _ = local_order(ir, best, problem, 'integrated')
        allocation = SearchBudget(3, order)
        allocation.structural_exhausted = True  # initialization is frozen
        for record, _, _ in seed_records:
            allocation.add(record, best)
        queues, expanded = {}, {}
        while available():
            family, parent = allocation.choose(best)
            if family is None:
                break
            key = parent['record_path'], family
            if key not in queues:
                queues[key] = deque()
            pool = queues[key]
            if not pool and expanded.get(key, 0) < 2:
                index = expanded.get(key, 0)
                expanded[key] = index+1
                def old_batch():
                    with gzip.open(parent['result_path'], 'rt') as f:
                        raw = json.load(f)
                    return candidates(ir, read_json(parent['plan_path']), raw,
                                      problem, cores, family, index, 24)[0]
                batch, _ = generate('legacy_'+family, old_batch, parent['record_path'])
                pool.extend(batch or [])
            if not pool:
                if expanded.get(key, 0) >= 2:
                    allocation.exhaust(family, parent)
                continue
            candidate = pool.popleft()
            before = score(best)[0]
            t = time.monotonic()
            trial = apply(candidate, family, parent['record_path'])
            if trial is None:
                continue
            r = trial['record']
            if r['status'] == 'success':
                trial['local_accepted'] = score(r) < score(parent)
                allocation.add(r, best)
            allocation.observe(family, parent, before, score(best)[0], time.monotonic()-t)
            branch_events.append(dict(event='legacy_visit', parent_record=parent['record_path'],
                                     family=family, accepted=trial['accepted']))
            checkpoint()
        return checkpoint(True)

    allocation = PersistentBudget(3, 2)
    for record, plan, name in seed_records:
        allocation.add_seed(record, structural_signature(plan, problem), name)
    allocation.start()
    family_order = (['joint', 'legacy', 'insertion'] if problem == 1 else
                    ['insertion', 'joint', 'cache', 'trace'] if problem == 3 else
                    ['insertion', 'joint', 'trace'])

    def load_frame(frame):
        if frame.plan is None:
            frame.plan = read_json(frame.record['plan_path'])
        if frame.raw is None:
            with gzip.open(frame.record['result_path'], 'rt') as handle:
                frame.raw = json.load(handle)
            if frame.raw['makespan'] != score(frame.record)[0] or frame.raw['num_cores'] != cores:
                raise ValueError('paid parent/result mismatch')

    def stream(frame, family):
        load_frame(frame)
        if family == 'insertion':
            from event_frontier import candidates
            # One complete fixed-ownership insertion. Other global placements
            # belong in the initializer, not repeated on every local parent.
            yield next(candidates(ir, problem, cores, frame.plan, time.monotonic()+12))
        elif family == 'joint':
            if frame.observation is None and problem != 1:
                return
            if problem == 1:
                from persistent_p1_moves import iter_candidates
            else:
                from persistent_p23_moves import iter_candidates
            return (yield from iter_candidates(ir, frame.plan, frame.raw, cores,
                round_index=frame.depth+2*frame.joint_round, observation=frame.observation, seconds=24))
        elif family == 'legacy':
            from p1_task_refine import generate as task_candidates
            yield from task_candidates(ir, frame.plan, frame.raw, seed=17+frame.depth)
        elif family == 'trace':
            from advanced_solver.trace_refine import generate_trace_candidates
            for index in range(2):
                pool, _ = generate_trace_candidates(ir, frame.plan, frame.raw,
                    num_cores=cores, max_candidates=2, round_index=frame.depth+index, seed=17)
                yield from pool
        elif family == 'cache':
            from advanced_solver.cache_refine import generate_cache_candidates
            for index in range(2):
                pool, _ = generate_cache_candidates(ir, frame.plan, frame.raw,
                    num_cores=cores, max_candidates=2, round_index=frame.depth+index, seed=17)
                yield from pool

    def proposal(frame):
        # A single completed candidate per turn. Duplicate/invalid candidates
        # do not consume calls; finite family streams and the deadline bound it.
        for _ in range(len(family_order)*3):
            if not available():
                break
            family = frame.next_family(family_order)
            if family in frame.exhausted:
                continue
            if family == 'joint' and not frame.observation_attempted:
                frame.observation_attempted = True
                def observe_frame():
                    load_frame(frame)
                    from wait_observation import observe
                    return observe(ir, frame.plan, frame.raw, problem)
                frame.observation, _ = generate('observe_paid_parent', observe_frame,
                                                frame.record['record_path'])
                if frame.observation is None:
                    if problem != 1:
                        frame.exhausted.add(family)
                        continue
                    branch_events.append(dict(event='observation_fallback', parent_record=frame.record['record_path'],
                        scope='P1 Task-release trace only; internal compiled causality unavailable'))
                else:
                    branch_events.append(dict(event='observe', parent_record=frame.record['record_path'],
                        depth=frame.depth, observation=frame.observation['summary']))
            if family not in frame.streams:
                frame.streams[family] = stream(frame, family)
            candidate, status = generate('persistent_'+family,
                lambda: next(frame.streams[family]), frame.record['record_path'])
            if candidate is None:
                if family == 'joint' and status == 'exhausted' and frame.joint_round < 1:
                    frame.joint_round += 1
                    frame.streams.pop(family, None)
                    continue
                frame.exhausted.add(family)
                continue
            if exact_signature(candidate['plan']) in seen:
                skips.append(dict(name=candidate['name'], parent_record=frame.record['record_path'],
                                  reason='exact_ordered_duplicate', phase=family))
                continue
            return candidate, family
        return None, None

    while available():
        branch = allocation.choose()
        if branch is None:
            break
        if not branch.frames:
            branch.frames.append(Frame(branch.record, branch.depth))
            branch.frames.extend(Frame(x['record'], 0, stale_lease=1) for x in branch.alternate_seeds)
        current = next((f for f in branch.frames if f.record['record_path']==branch.record['record_path']), None)
        if current is None:
            current = Frame(branch.record, branch.depth)
            branch.frames.append(current)
        stale = [f for f in branch.frames if f is not current and f.stale_lease > 0]
        frame = stale[0] if stale and branch.visits % 2 == 0 else current
        candidate, family = None, None
        eligible = [frame]+[f for f in [current]+stale if f is not frame]
        for proposed_frame in eligible:
            if len(proposed_frame.exhausted) >= len(family_order):
                proposed_frame.stale_lease = 0
                continue
            candidate, family = proposal(proposed_frame)
            if candidate is not None:
                frame = proposed_frame
                break
            if len(proposed_frame.exhausted) >= len(family_order):
                proposed_frame.stale_lease = 0
        if candidate is None:
            branch.exhausted = all(len(f.exhausted)>=len(family_order) for f in eligible)
            checkpoint()
            continue
        old_best = best
        trial = apply(candidate, family, frame.record['record_path'], branch.number, frame.depth,
                      parent_makespan=score(frame.record)[0])
        if trial is None:
            continue
        frame.served(family)
        if frame is not current:
            frame.stale_lease = max(0, frame.stale_lease-1)
        r = trial['record']
        trial['local_accepted'] = r['status']=='success' and score(r) < score(frame.record)
        shape = structural_signature(candidate['plan'], problem) if r['status']=='success' else None
        advanced = allocation.observe(branch, r, shape)
        trial['lineage_advanced'] = advanced
        if advanced:
            # Carry the previous branch's visit count and grant a finite one-
            # call continuation to the actual old queue, bound to its own trace.
            # Bind to the iterator that produced the child, even when this was
            # an old alternate frame. A lease on an unrelated next family is
            # not a continuation of its pending regional/FIFO queue.
            frame.protect_queue(family)
            if current is not frame:
                current.protect_queue(current.last_family)
            branch.frames.append(Frame(r, frame.depth+1))
            branch.depth = frame.depth+1
            branch_events.append(dict(event='advance', lineage=branch.number,
                parent_record=frame.record['record_path'], record=r['record_path'],
                depth=branch.depth, global_improvement=best is not old_best,
                family=family, visits_carried=branch.visits))
        elif trial['local_accepted']:
            # An alternate order may improve without overtaking this lineage's
            # fastest frame. Preserve its own newly observed continuation.
            frame.protect_queue(family)
            branch.frames.append(Frame(r, frame.depth+1, stale_lease=1))
            branch.stagnant = 0
            branch_events.append(dict(event='advance_alternate', lineage=branch.number,
                parent_record=frame.record['record_path'], record=r['record_path'],
                depth=frame.depth+1, family=family, visits_carried=branch.visits))
        # Completed leases do not keep old observations/streams alive forever.
        branch.frames[:] = [f for f in branch.frames
                            if f.record['record_path']==branch.record['record_path'] or f.stale_lease>0]
        branch_events.append(dict(event='visit', lineage=branch.number, family=family,
            parent_record=frame.record['record_path'], record=r.get('record_path'),
            protected_remaining=branch.protected_remaining, depth=frame.depth,
            local_accepted=trial['local_accepted'], global_accepted=trial['accepted'],
            stale_parent=frame is not current, status=r['status']))
        checkpoint()
    branch_events.extend(allocation.events)
    assert len(calls) <= budget
    assert allocation.turn == sum(c['phase']!='initialization' for c in calls)
    return checkpoint(True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--case', type=int, required=True, choices=range(1, 101))
    p.add_argument('--problem', type=int, required=True, choices=(1, 2, 3))
    p.add_argument('--cores', type=int, default=5, choices=range(1, 6))
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--budget', type=int, default=24)
    p.add_argument('--seconds', type=float, default=240)
    p.add_argument('--timeout', type=float, default=60)
    p.add_argument('--variant', choices=('persistent', 'legacy'), default='persistent')
    a = p.parse_args()
    s = run(f'case_{a.case:03d}', a.problem, a.cores, a.out, a.budget, a.seconds, a.timeout, a.variant)
    print(json.dumps(dict(calls=s['logical_calls'], best=score(s['best_record']) if s['best_record'] else None,
                         seconds=s['elapsed_seconds'], complete=s['complete']), ensure_ascii=False))
