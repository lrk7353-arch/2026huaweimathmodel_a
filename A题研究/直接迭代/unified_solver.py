"""Unified cold/warm search for all three official scenes, one total budget.

No implicit historical incumbents or result-cache reuse. An explicitly supplied
plan is re-evaluated and charged. All candidate failures remain in the ledger.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

from common_run import DATA, GraphIR, atomic_json, evaluate, read_json, score, validate_plan
from unified_structure import Structure, candidate_stream


def signature(plan):
    # Preserve mapping insertion order: official builders can observe it.
    return hashlib.sha256(json.dumps(plan, separators=(',', ':')).encode()).hexdigest()


def seeds(ir, scene, cores, seed):
    from advanced_solver.component_baseline import generate_component_candidates
    component, _ = generate_component_candidates(ir, cores, max_candidates=3, seed=seed)
    for c in component[:1]:
        yield {**c, 'metadata': {**c['metadata'], 'new_strategy': False}}
    if scene == 1:
        from p1_selective import generate_selective_candidates
        other, _ = generate_selective_candidates(ir, cores, max_candidates=12, seed=seed)
    else:
        from advanced_solver.operation_assign import generate_operation_candidates
        other, _ = generate_operation_candidates(ir, cores, max_candidates=12, seed=seed)
    for c in other + component[1:]:
        yield {**c, 'metadata': {**c['metadata'], 'new_strategy': False}}


def solve_unified(graph, scene, cores, out, *, seconds=180, call_budget=8, seed=17,
                  incumbent=None, variant='v2', evaluation_timeout=60):
    if scene not in (1, 2, 3) or type(cores) is not int or not 1 <= cores <= 5:
        raise ValueError('scene 1..3 and cores 1..5 required')
    if seconds <= 0 or type(call_budget) is not int or call_budget < 1 or evaluation_timeout <= 0:
        raise ValueError('positive time/call budgets required')
    if variant not in ('v2', 'baseline'):
        raise ValueError('variant must be v2 or baseline')
    started = time.monotonic()
    deadline = started + seconds
    graph, out = Path(graph).resolve(), Path(out).resolve()
    if out == DATA or DATA in out.parents:
        raise ValueError('outputs must be outside official data')
    out.mkdir(parents=True, exist_ok=False)
    ir = GraphIR.from_path(graph)
    structure = Structure(ir)
    graph_hash = hashlib.sha256(graph.read_bytes()).hexdigest()
    calls, generated, errors, seen = [], [], [], set()
    best = None
    stop_reason = 'pool_exhausted'
    generation_seconds = time.monotonic() - started
    old = iter(seeds(ir, scene, cores, seed))
    new = iter(candidate_stream(structure, scene, cores, deadline)) if variant == 'v2' else iter(())
    pending = []
    if incumbent is not None:
        pending.append(dict(name='explicit_incumbent', plan=read_json(incumbent) if isinstance(incumbent, (str, Path)) else incumbent,
                            metadata=dict(family='incumbent', new_strategy=False)))
    exhausted = set()
    proposed = 0
    while len(calls) < call_budget:
        remaining = deadline - time.monotonic()
        if remaining <= .25:
            stop_reason = 'time_budget'
            break
        # Slots 1 and 3 use mature starting families; slot 2 is reserved for a
        # scene-specific structural candidate. Failed/duplicate generation does
        # not consume a logical official call, but does consume elapsed time.
        source = 'old' if variant == 'baseline' or len(calls) in (0, 2) else 'new'
        if source in exhausted:
            source = 'new' if source == 'old' else 'old'
        generation_start = time.monotonic()
        if pending:
            c = pending.pop(0)
            source = 'explicit'
        elif source in exhausted:
            break
        else:
            try:
                c = next(old if source == 'old' else new)
            except StopIteration:
                exhausted.add(source)
                continue
            except (ValueError, TimeoutError) as error:
                errors.append(dict(source=source, error=str(error)))
                exhausted.add(source)
                continue
        generation_seconds += time.monotonic() - generation_start
        proposed += 1
        identity = signature(c['plan'])
        if identity in seen:
            generated.append(dict(name=c['name'], decision='duplicate', source=source))
            if proposed > 200:
                break
            continue
        seen.add(identity)
        try:
            validate_plan(ir, c['plan'])
            if len(c['plan']['core_schedules']) != cores:
                raise ValueError('core count mismatch')
        except ValueError as error:
            generated.append(dict(name=c['name'], decision='invalid_structure', error=str(error), source=source))
            continue
        remaining = deadline - time.monotonic()
        successes = [x['record']['elapsed_seconds'] for x in calls if x['record']['status'] == 'success']
        # Preserve useful search time; never start an expected overrun when a
        # verified return value already exists. This is an estimate, not a bound.
        expected = max(successes[-3:], default=0) * 1.25
        if remaining <= .25 or (best and expected > remaining):
            stop_reason = 'insufficient_remaining_time'
            generated.append(dict(name=c['name'], decision='time_reserve', estimated_seconds=expected, source=source))
            break
        call_cap = min(evaluation_timeout, remaining - .15)
        # Before a first success use up to half the total deadline to leave a
        # different structural fallback a real opportunity.
        if not best and call_budget > 1:
            call_cap = min(call_cap, max(1., seconds * .5))
        rec = evaluate(graph, c['plan'], scene, out / 'evaluations', timeout=call_cap,
                       config_path=graph.parent / 'config.txt')
        accepted = rec['status'] == 'success' and (best is None or score(rec) < score(best['record']))
        row = dict(name=c['name'], source=source, metadata=c['metadata'], record=rec,
                   accepted=accepted, generation_seconds=time.monotonic() - generation_start - rec['elapsed_seconds'])
        calls.append(row)
        if accepted:
            best = dict(name=c['name'], record=rec)
            atomic_json(out / 'best.plan.json', c['plan'])
        atomic_json(out / 'progress.json', dict(calls=len(calls), best=best, last_status=rec['status'],
            elapsed_seconds=time.monotonic() - started, generation_seconds=generation_seconds))
    if len(calls) >= call_budget:
        stop_reason = 'call_budget'
    if hashlib.sha256(graph.read_bytes()).hexdigest() != graph_hash:
        raise AssertionError('original graph changed')
    result = dict(case=graph.stem, problem=scene, num_cores=cores, variant=variant,
        mode='warm_explicit_charged' if incumbent is not None else 'cold', seed=seed,
        budget=call_budget, seconds=seconds, evaluation_timeout=evaluation_timeout,
        graph_sha256=graph_hash, profile=structure.profile, best=best,
        best_record=best['record'] if best else None, evaluations=calls, generated=generated,
        generation_errors=errors, logical_calls=len(calls), new_calls=sum(not r['record']['cache_hit'] for r in calls),
        generation_seconds=generation_seconds, elapsed_seconds=time.monotonic() - started,
        stop_reason=stop_reason, returned_valid=best is not None,
        scope='one charged generation/evaluation walltime budget; no historical implicit input; proxy is ranking only')
    atomic_json(out / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', type=int, required=True, choices=range(1, 101))
    parser.add_argument('--problem', type=int, required=True, choices=(1, 2, 3))
    parser.add_argument('--cores', type=int, default=5, choices=range(1, 6))
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=180)
    parser.add_argument('--budget', type=int, default=8)
    parser.add_argument('--seed', type=int, default=17)
    parser.add_argument('--evaluation-timeout', type=float, default=60)
    parser.add_argument('--incumbent-plan', type=Path)
    parser.add_argument('--variant', choices=('v2', 'baseline'), default='v2')
    a = parser.parse_args()
    s = solve_unified(DATA / f'case_{a.case:03d}.json', a.problem, a.cores, a.out,
        seconds=a.seconds, call_budget=a.budget, seed=a.seed, incumbent=a.incumbent_plan,
        variant=a.variant, evaluation_timeout=a.evaluation_timeout)
    print(json.dumps({k: s[k] for k in ('case', 'problem', 'num_cores', 'logical_calls', 'elapsed_seconds', 'returned_valid', 'stop_reason')}))
    return 0 if s['returned_valid'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
