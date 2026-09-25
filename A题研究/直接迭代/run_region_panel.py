"""Predeclared warm-start G/R/RG comparison against checked-in round-six plans."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from common_run import *
from run_region_refine import run


def one(case, root, budget, seconds):
    root = Path(root)/case
    plan = R/'直接迭代/第六轮成果/方案/p1/n5'/(case+'_multicore_res.json')
    started = time.monotonic()
    old = run_candidate(case, 1, 5, read_json(plan), root/'baseline', timeout=min(60, seconds))
    if old['status'] != 'success':
        raise RuntimeError(case + ': baseline failed: ' + old['status'])
    remaining = seconds - (time.monotonic()-started)
    if remaining <= 0:
        raise RuntimeError(case + ': baseline exhausted time window')
    rows = []
    for method in ('tasks', 'regions', 'joint'):
        s = run(case, old, root/method, budget-1, remaining,
                method=method, evaluation_dir=root/method/'evaluations')
        row = dict(case=case, problem=1, cores=5, method=method,
                   before=s['before'], after=s['after'],
                   logical_calls=1+s['logical_calls'],
                   new_candidate_calls=s['new_calls'],
                   baseline_new_calls=int(not old['cache_hit']),
                   generation_seconds=s['generation_seconds'],
                   search_seconds=s['elapsed_seconds'],
                   skipped=len(s['skipped']),
                   timeouts=sum(t['record']['status']=='timeout' for t in s['evaluations']),
                   errors=sum(t['record']['status'] not in ('success','timeout') for t in s['evaluations']),
                   summary=str(root/method/'summary.json'),
                   stop_reason=s['stop_reason'])
        rows.append(row)
        write_csv(root/'results.csv', rows)
        print(json.dumps(row), flush=True)
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases', default='16,62,63,100')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--budget', type=int, default=8)
    p.add_argument('--seconds', type=float, default=180)
    p.add_argument('--workers', type=int, default=2)
    a = p.parse_args()
    ids = [int(i) for i in a.cases.split(',')]
    if not ids or len(set(ids)) != len(ids) or any(i not in range(1,101) for i in ids):
        p.error('unique cases 1..100 required')
    if a.budget < 2 or a.seconds <= 0 or a.workers < 1:
        p.error('budget >= 2, positive seconds and workers required')
    out = a.out.resolve()
    if out == DATA or DATA in out.parents:
        p.error('output must be outside official data')
    out.mkdir(parents=True, exist_ok=False)
    cases = [f'case_{i:03d}' for i in ids]
    atomic_json(out/'plan.json', dict(cases=cases, methods=['tasks','regions','joint'],
                budget=a.budget, seconds=a.seconds, workers=a.workers,
                upstream_commit='18e0957475b20e9a3af52ab335dccb2611beefc9',
                note='Each arm charges one common baseline; fresh candidate caches per arm. '
                     'Targeted mechanism development, not independent or from-scratch ranking.'))
    rows = []
    start = time.monotonic()
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        futures = [pool.submit(one, c, out, a.budget, a.seconds) for c in cases]
        for future in as_completed(futures):
            rows.extend(future.result())
            write_csv(out/'results.csv', rows)
            atomic_json(out/'progress.json', dict(completed=len(rows), expected=3*len(cases)))
    atomic_json(out/'summary.json', dict(completed=True, rows=rows,
                elapsed_seconds=time.monotonic()-start,
                total_logical_calls=sum(r['logical_calls'] for r in rows),
                actual_new_evaluations=sum(r['new_candidate_calls'] for r in rows)+len(cases)))


if __name__ == '__main__':
    main()
