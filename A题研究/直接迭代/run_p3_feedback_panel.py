"""Compare P3 controllers under one call/time cap from common recorded incumbents.

Fresh mode isolates candidate evaluations per slot. The supplied incumbent and
its observation are still a warm starting point, not a from-scratch solve.
"""
import argparse
import random
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

from common_run import *


METHODS = ('legacy', 'read_order', 'joint', 'interleave', 'feedback')


def one(case, old, out, method, budget, seconds, cores, seed, fresh, per_call_timeout=60):
    out = Path(out)
    evaluation_dir = out / 'evaluations' if fresh else None
    started = time.monotonic()
    try:
        if seed != 17 and method in ('legacy', 'read_order', 'joint'):
            raise ValueError('Legacy/read_order/joint require seed 17.')
        if per_call_timeout <= 0:
            raise ValueError('per_call_timeout must be positive.')
        if method in ('interleave', 'feedback') and per_call_timeout != 60:
            raise ValueError('Interleave/feedback currently require a 60-second per-call timeout.')
        if method in ('interleave', 'feedback'):
            from p3_feedback import run
            result = run(case, old, out, budget, seconds, cores=cores,
                         policy=method, evaluation_dir=evaluation_dir, seed=seed)
        elif method == 'joint':
            from p3_joint import run
            result = run(case, old, out, budget, seconds, cores=cores,
                         evaluation_dir=evaluation_dir, per_call_timeout=per_call_timeout)
        elif method == 'read_order':
            from p3_read_order import run
            result = run(case, old, out, budget, seconds, cores=cores,
                         evaluation_dir=evaluation_dir, per_call_timeout=per_call_timeout)
        elif method == 'legacy':
            from run_p3_refine import run
            # All methods get exactly the same P3 incumbent; no P2 inheritance.
            result = run(case, old, None, out, budget, seconds, cores=cores,
                         evaluation_dir=evaluation_dir, per_call_timeout=per_call_timeout)
        else:
            raise ValueError(f'Unknown method: {method}')
        best = result['best_record']
        result.update(method=method, seed=seed, fresh_evaluations=fresh,
                      status='success', winner_plan=best['plan_path'],
                      per_call_timeout=per_call_timeout,
                      evaluation_dir=str(evaluation_dir) if evaluation_dir else None)
        atomic_json(out / 'summary.json', result)
        return dict(case=case, method=method, cores=cores, seed=seed,
                    status='success', before=result['before'], after=result['after'],
                    logical_calls=result['logical_calls'], new_calls=result['new_calls'],
                    elapsed_seconds=result['elapsed_seconds'], stop_reason=result['stop_reason'],
                    winner_plan=best['plan_path'], summary_path=str(out / 'summary.json'),
                    fresh_evaluations=fresh, error_type=None, error=None)
    except Exception as exc:
        # Preserve partial progress and the complete error without pretending the
        # failed search completed successfully with the input plan as its winner.
        error = dict(case=case, problem=3, num_cores=cores, method=method, seed=seed,
                     status='error', before=score(old)[0], after=None,
                     logical_calls=None, new_calls=None, winner_plan=None,
                     elapsed_seconds=time.monotonic() - started, stop_reason='exception',
                     fresh_evaluations=fresh, error_type=type(exc).__name__,
                     per_call_timeout=per_call_timeout,
                     error=str(exc), traceback=traceback.format_exc())
        if (out / 'progress.json').exists():
            try:
                error['partial_progress'] = read_json(out / 'progress.json')
            except Exception as progress_exc:
                error['partial_progress_error'] = str(progress_exc)
        atomic_json(out / 'summary.json', error)
        return dict(case=case, method=method, cores=cores, seed=seed,
                    **{name: error[name] for name in ('status', 'before', 'after',
                       'logical_calls', 'new_calls', 'elapsed_seconds', 'stop_reason',
                       'winner_plan', 'fresh_evaluations', 'error_type', 'error')},
                    summary_path=str(out / 'summary.json'))


def case_list(value):
    try:
        values = [int(item.strip()) for item in value.split(',')]
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Cases must be comma-separated integers.') from exc
    if not values or len(set(values)) != len(values) or any(not 1 <= case <= 100 for case in values):
        raise argparse.ArgumentTypeError('Use unique case numbers from 1 to 100.')
    return [f'case_{case:03d}' for case in values]


def method_list(value):
    values = [item.strip() for item in value.split(',')]
    if not values or len(set(values)) != len(values) or any(method not in METHODS for method in values):
        raise argparse.ArgumentTypeError('Use unique methods from: ' + ','.join(METHODS))
    return values


def main(args):
    out = Path(args.out).resolve()
    if out == DATA or DATA in out.parents:
        raise ValueError('Outputs must be outside official data.')
    if out.exists():
        raise ValueError('Use a new output directory.')
    if args.budget < 1 or args.seconds <= 0 or args.workers < 1 or args.per_call_timeout <= 0:
        raise ValueError('Budget, seconds, workers and per-call timeout must be positive.')
    if args.seed != 17 and any(method in ('legacy', 'read_order', 'joint') for method in args.methods):
        raise ValueError('Legacy/read_order/joint have fixed seed 17; non-17 panels may only use interleave/feedback.')
    if args.per_call_timeout != 60 and any(method in ('interleave', 'feedback') for method in args.methods):
        raise ValueError('Interleave/feedback currently require --per-call-timeout 60.')
    snapshot = read_json(args.before)
    before = {key(record): record for record in snapshot['records'] if record.get('status') == 'success'}
    for case in args.cases:
        record = before.get((case, 3, args.cores))
        if record is None or record.get('status') != 'success':
            raise ValueError(f'Missing successful P3 incumbent: {case}, cores={args.cores}')
    out.mkdir(parents=True, exist_ok=False)
    jobs = [(case, method) for case in args.cases for method in args.methods]
    random.Random(args.seed).shuffle(jobs)
    atomic_json(out / 'plan.json', dict(
        before=str(Path(args.before).resolve()), cases=args.cases, methods=args.methods,
        cores=args.cores, budget=args.budget, seconds=args.seconds, workers=args.workers,
        per_call_timeout=args.per_call_timeout,
        seed=args.seed, fresh_evaluations=args.fresh_evaluations, jobs=jobs,
        scope='Common recorded warm P3 incumbent; one total call/time cap; no P2 inheritance; '
              'fresh mode isolates new candidate caches but reuses the input plan observation.'))
    started = time.monotonic()
    rows = []

    def save(complete=False):
        ordered = sorted(rows, key=lambda row: (row['case'], args.methods.index(row['method'])))
        result = dict(complete=complete, completed=len(rows), expected=len(jobs),
                      successful=sum(row['status'] == 'success' for row in rows),
                      failed=sum(row['status'] != 'success' for row in rows),
                      cases=args.cases, methods=args.methods, cores=args.cores,
                      budget=args.budget, seconds=args.seconds, seed=args.seed,
                      per_call_timeout=args.per_call_timeout,
                      fresh_evaluations=args.fresh_evaluations,
                      rows=ordered, wall_seconds=time.monotonic() - started)
        write_csv(out / 'results.csv', ordered)
        atomic_json(out / ('summary.json' if complete else 'progress.json'), result)
        return result

    save()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(one, case, before[case, 3, args.cores], out / case / method,
                               method, args.budget, args.seconds, args.cores, args.seed,
                               args.fresh_evaluations, args.per_call_timeout) for case, method in jobs]
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            save()
            print(json.dumps(row, ensure_ascii=False), flush=True)
    summary = save(complete=True)
    print(json.dumps(dict(complete=True, completed=summary['completed'],
                         failed=summary['failed'], wall_seconds=summary['wall_seconds'])), flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', required=True)
    parser.add_argument('--cases', type=case_list, required=True)
    parser.add_argument('--methods', type=method_list, default=list(METHODS))
    parser.add_argument('--cores', type=int, choices=range(1, 6), default=5)
    parser.add_argument('--budget', type=int, default=12)
    parser.add_argument('--seconds', type=float, default=120)
    parser.add_argument('--per-call-timeout', type=float, default=60)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--out', required=True)
    parser.add_argument('--seed', type=int, default=17)
    parser.add_argument('--fresh-evaluations', action='store_true')
    parsed = parser.parse_args()
    try:
        main(parsed)
    except ValueError as exc:
        parser.error(str(exc))
