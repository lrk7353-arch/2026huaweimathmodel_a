"""Resumable per-slot unified runs; every slot owns its evaluation processes.

The manifest pins inputs, source hashes and search settings. A completed slot is
reused only under that exact manifest. An incomplete slot is never overwritten.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
import hashlib
import json
from pathlib import Path
import time

from common_run import DATA, R, atomic_json, read_json, write_csv
from unified_solver import solve_unified


def worker(job):
    case, p, n, variant, out, budget, seconds, timeout, seed = job
    summary = Path(out) / 'summary.json'
    if summary.exists():
        return read_json(summary)
    return solve_unified(DATA / (case + '.json'), p, n, out, call_budget=budget,
        seconds=seconds, evaluation_timeout=timeout, seed=seed, variant=variant)


def summarize(s):
    rec = s['best_record']
    old = [x['record']['metrics']['makespan'] for x in s['evaluations']
           if not x['metadata'].get('new_strategy') and x['record']['status'] == 'success']
    return dict(case=s['case'], problem=s['problem'], cores=s['num_cores'], variant=s['variant'],
        valid=s['returned_valid'], makespan=rec['metrics']['makespan'] if rec else None,
        best_name=s['best']['name'] if s['best'] else None, best_seed=min(old) if old else None,
        improvement_vs_evaluated_seeds=1 - rec['metrics']['makespan'] / min(old) if old and rec else None,
        calls=s['logical_calls'], new_calls=s['new_calls'], seconds=s['elapsed_seconds'],
        generation_seconds=s['generation_seconds'], new_strategy_calls=sum(x['metadata'].get('new_strategy', False) for x in s['evaluations']),
        failures=sum(x['record']['status'] != 'success' for x in s['evaluations']),
        timeouts=sum(x['record']['status'] == 'timeout' for x in s['evaluations']),
        stop_reason=s['stop_reason'], best_record_path=rec.get('record_path') if rec else None)


def run(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    cases = [f'case_{i:03d}' for i in args.cases]
    source_files = [p for folder in (R / 'solver', R / 'advanced_solver', R / '精修求解器', R / '探索', Path(__file__).parent)
                    for p in folder.glob('*.py')]
    manifest = dict(cases=cases, problems=args.problems, cores=args.cores, variants=args.variants,
        budget=args.budget, seconds=args.seconds, timeout=args.timeout, seed=args.seed, workers=args.workers,
        sources={str(p.relative_to(R)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(source_files))},
        inputs={c: hashlib.sha256((DATA / (c + '.json')).read_bytes()).hexdigest() for c in cases},
        config=hashlib.sha256((DATA / 'config.txt').read_bytes()).hexdigest())
    if (out / 'manifest.json').exists() and read_json(out / 'manifest.json') != manifest:
        raise ValueError('manifest changed; use a new output directory')
    atomic_json(out / 'manifest.json', manifest)
    jobs = [(c, p, n, v, str(out / 'slots' / c / f'p{p}_n{n}' / v), args.budget, args.seconds, args.timeout, args.seed)
            for c in cases for n in args.cores for p in args.problems for v in args.variants]
    # Size ordering reduces the long-job tail without using scores or graph IDs.
    jobs.sort(key=lambda j: -(DATA / (j[0] + '.json')).stat().st_size)
    rows, errors = [], []
    start = time.monotonic()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        pending = {pool.submit(worker, j): j for j in jobs}
        for future in as_completed(pending):
            job = pending[future]
            try:
                row = summarize(future.result())
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
            except Exception as error:
                errors.append(dict(job=job[:4], error=repr(error)))
                print(json.dumps(errors[-1]), flush=True)
            write_csv(out / 'results.csv', sorted(rows, key=lambda r: (r['case'], r['problem'], r['cores'], r['variant'])))
            atomic_json(out / 'progress.json', dict(completed=len(rows), total=len(jobs), errors=errors,
                elapsed_seconds=time.monotonic() - start))
    result = dict(completed=len(rows), total=len(jobs), errors=errors, elapsed_seconds=time.monotonic() - start,
        official_calls=sum(r['new_calls'] for r in rows), valid_slots=sum(r['valid'] for r in rows),
        interpretation='Improvement vs evaluated seeds is mechanism evidence, not equal-budget baseline dominance.')
    atomic_json(out / 'completion.json', result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    ints = lambda s: [int(x) for x in s.split(',')]
    p.add_argument('--cases', type=ints, default=list(range(1, 101)))
    p.add_argument('--problems', type=ints, default=[1, 2, 3])
    p.add_argument('--cores', type=ints, default=[5])
    p.add_argument('--variants', type=lambda s: s.split(','), default=['v2'])
    p.add_argument('--budget', type=int, default=8)
    p.add_argument('--seconds', type=float, default=180)
    p.add_argument('--timeout', type=float, default=60)
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--seed', type=int, default=17)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    if any(c not in range(1, 101) for c in a.cases) or any(p not in (1, 2, 3) for p in a.problems) or any(n not in range(1, 6) for n in a.cores):
        p.error('invalid graph/scene/core range')
    if min(a.budget, a.seconds, a.timeout, a.workers) <= 0:
        p.error('positive budget/time/workers required')
    print(json.dumps(run(a), ensure_ascii=False))


if __name__ == '__main__':
    main()
