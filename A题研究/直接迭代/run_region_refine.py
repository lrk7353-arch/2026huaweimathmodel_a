"""Bounded opt-in P1 regional refinement; all methods retain the input best."""
import gzip
import time
from common_run import *
from p1_local_regions import generate, exact_key


def run(case, old, out, budget=7, seconds=180, cores=5, seed=17,
        method='joint', evaluation_dir=None):
    if key(old) != (case, 1, cores):
        raise ValueError('incumbent case/problem/core mismatch')
    if method not in ('joint', 'regions', 'tasks') or budget < 1 or seconds <= 0:
        raise ValueError('invalid method or budget')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    deadline = start + seconds
    evdir = Path(evaluation_dir) if evaluation_dir is not None else out/'evaluations'
    ir = GraphIR.from_path(DATA/(case+'.json'))
    plan = read_json(old['plan_path'])
    if method == 'tasks':
        from p1_task_refine import generate as task_candidates
        from p1_portfolio import refinement_caps
        with gzip.open(old['result_path'], 'rt') as f:
            raw = json.load(f)
        caps = refinement_caps(plan, cores)
        candidates = task_candidates(ir, plan, raw, seed, merge_caps=caps) if caps else []
        candidates += task_candidates(ir, plan, raw, seed)
        diag = {'method': method, 'generated': len(candidates), 'caps': caps}
    else:
        candidates, diag = generate(ir, plan, max_moves=max(8, budget),
                                    seed=seed, combine=method == 'joint')
    generation_seconds = time.monotonic() - start
    atomic_json(out/'candidates.json',
                [dict(name=c['name'], **c['metadata']) for c in candidates])
    best, best_plan, trials, skipped = old, plan, [], []
    seen = {exact_key(plan)}
    # No pruning of unmerged intermediates: every candidate is complete here.
    for c in candidates:
        if len(trials) >= budget or time.monotonic() >= deadline:
            break
        signature = exact_key(c['plan'])
        if signature in seen:
            skipped.append(dict(name=c['name'], reason='exact_duplicate'))
            continue
        seen.add(signature)
        meta = c['metadata']
        if meta['lower_bound'] > score(best)[0]:
            skipped.append(dict(name=c['name'], reason='necessary_bound',
                                bound=meta['lower_bound']))
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        rec = evaluate(DATA/(case+'.json'), c['plan'], 1, evdir,
                       timeout=min(60, remaining), config_path=DATA/'config.txt')
        if rec['status'] == 'success' and meta['lower_bound'] > score(rec)[0]:
            raise AssertionError('unsafe final-candidate bound')
        accepted = rec['status'] == 'success' and score(rec) < score(best)
        trials.append(dict(name=c['name'], metadata=meta, record=rec, accepted=accepted))
        if accepted:
            best, best_plan = rec, c['plan']
        atomic_json(out/'progress.json',
                    dict(case=case, method=method, calls=len(trials),
                         before=score(old)[0], after=score(best)[0]))
    atomic_json(out/'best.plan.json', best_plan)
    result = dict(case=case, problem=1, num_cores=cores, method=method,
                  before=score(old)[0], after=score(best)[0], best_record=best,
                  evaluations=trials, skipped=skipped, diagnostics=diag,
                  budget=budget, logical_calls=len(trials),
                  new_calls=sum(not t['record']['cache_hit'] for t in trials),
                  generation_seconds=generation_seconds,
                  elapsed_seconds=time.monotonic()-start,
                  stop_reason='time_budget' if time.monotonic() >= deadline else 'budget_or_pool',
                  scope='fixed-incumbent extra-budget refinement; not a from-scratch comparison')
    atomic_json(out/'summary.json', result)
    return result
