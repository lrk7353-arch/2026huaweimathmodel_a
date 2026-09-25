"""Serial P3 CLI replay/refinement from exported plans, without historical caches.

--plans is the exported result root containing
    方案/p3/n5/case_XXX_multicore_res.json
It is not the n5 directory. Every slot starts a separate solve.py process and
reevaluates the same exported plan before refinement. The budget includes that
initial evaluation. Fresh directories exclude historical cache reuse; duplicate
candidates within one slot may still hit that slot's newly populated cache.
"""
import argparse
import math
import os
import random
import signal
import subprocess
import sys
import traceback
from collections import Counter

from common_run import *


METHODS = ('legacy', 'read_order', 'joint', 'interleave', 'feedback')
DEFAULT_METHODS = ('legacy', 'joint', 'feedback')
CLI = Path(__file__).resolve().with_name('solve.py')
WATCHDOG_GRACE_SECONDS = 60.0


def case_list(value):
    try:
        values = [int(item.strip()) for item in value.split(',')]
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Cases must be comma-separated integers.') from exc
    if not values or len(set(values)) != len(values) or any(not 1 <= value <= 100 for value in values):
        raise argparse.ArgumentTypeError('Use unique case numbers from 1 to 100.')
    return [f'case_{value:03d}' for value in values]


def method_list(value):
    methods = [item.strip() for item in value.split(',')]
    if not methods or len(set(methods)) != len(methods) or any(method not in METHODS for method in methods):
        raise argparse.ArgumentTypeError('Use unique methods from: ' + ','.join(METHODS))
    return methods


def stop_process_group(process):
    """Stop the CLI and any active evaluator worker, not just their parent."""
    if os.name == 'posix':
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        # The parent may already have exited while a descendant is alive.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        # Official workers have their own bounded timeout on non-POSIX hosts.
        process.kill()
    process.wait()


def collect_attempts(cli_out):
    """Recover completed records even when the CLI exits before summary output."""
    records, errors = [], []
    files = list((cli_out / 'initial_evaluation' / 'attempts').glob('*/record.json'))
    files.extend((cli_out / 'evaluations' / 'attempts').glob('*/record.json'))
    for path in sorted(files, key=lambda item: (item.stat().st_mtime_ns, str(item))):
        try:
            record = read_json(path)
            records.append(record)
        except Exception as exc:
            errors.append(dict(path=str(path), error_type=type(exc).__name__, error=str(exc)))
    return records, errors


def run_one(case, method, input_plan, slot, budget, seconds):
    slot = Path(slot)
    slot.mkdir(parents=True, exist_ok=False)
    cli_out = slot / 'solve'
    command = [sys.executable, '-B', str(CLI), '--case', str(int(case[5:])),
               '--problem', '3', '--cores', '5', '--seed', '17',
               '--incumbent-plan', str(input_plan), '--p3-refinement', method,
               '--fresh-evaluations', '--budget', str(budget), '--seconds', str(seconds),
               '--out', str(cli_out)]
    watchdog_seconds = seconds + WATCHDOG_GRACE_SECONDS
    atomic_json(slot / 'command.json', dict(command=command, cwd=str(CLI.parent),
                input_plan=str(input_plan), watchdog_seconds=watchdog_seconds))
    started = time.monotonic()
    process_wall = None
    returncode = None
    watchdog_timeout = False
    process = None
    error_type = error = error_traceback = None
    cli_summary = None
    try:
        env = os.environ.copy()
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        with (slot / 'stdout.log').open('wb') as stdout, (slot / 'stderr.log').open('wb') as stderr:
            process_started = time.monotonic()
            process = subprocess.Popen(command, cwd=str(CLI.parent), env=env,
                                       stdout=stdout, stderr=stderr,
                                       start_new_session=(os.name == 'posix'))
            try:
                returncode = process.wait(timeout=watchdog_seconds)
            except subprocess.TimeoutExpired:
                watchdog_timeout = True
                stop_process_group(process)
                returncode = process.returncode
            finally:
                process_wall = time.monotonic() - process_started
        if watchdog_timeout:
            raise TimeoutError(f'CLI exceeded {watchdog_seconds:g} seconds including watchdog grace.')
        if returncode != 0:
            raise RuntimeError(f'solve.py exited with code {returncode}; see stderr.log.')
        cli_summary = read_json(cli_out / 'summary.json')
        if cli_summary.get('best_record', {}).get('status') != 'success':
            raise ValueError('CLI did not return an officially successful best record.')
    except Exception as exc:
        error_type, error, error_traceback = type(exc).__name__, str(exc), traceback.format_exc()
    except BaseException:
        if process is not None and process.poll() is None:
            stop_process_group(process)
        raise

    attempts, record_errors = collect_attempts(cli_out)
    initial_records = [record for record in attempts
                       if Path(record.get('record_path', '')).is_relative_to(cli_out / 'initial_evaluation')]
    initial = initial_records[0] if len(initial_records) == 1 else None
    calls = []
    counts_complete = False
    if error is None:
        try:
            if initial is None or initial.get('status') != 'success':
                raise ValueError('Expected exactly one successful fresh initial evaluation.')
            if initial.get('cache_hit'):
                raise ValueError('Initial evaluation unexpectedly reused a cache entry.')
            if record_errors:
                raise ValueError('At least one saved evaluation record could not be read.')
            calls = [dict(name='initial_incumbent_evaluation', stage='initial', record=initial,
                          accepted=None)]
            calls.extend(dict(call, stage='refinement') for call in cli_summary.get('calls', []))
            logical = cli_summary['logical_calls']
            new = cli_summary['new_calls']
            if logical != len(calls) or not 1 <= logical <= budget:
                raise ValueError('CLI logical call count does not include exactly one initial evaluation.')
            if new != sum(not call['record']['cache_hit'] for call in calls):
                raise ValueError('CLI new-call count does not match saved calls.')
            if {call['record']['record_path'] for call in calls} != {record['record_path'] for record in attempts}:
                raise ValueError('Saved call route does not cover all completed evaluation attempts.')
            for call in calls:
                record = call['record']
                for field in ('record_path', 'plan_path'):
                    if not Path(record[field]).is_relative_to(cli_out):
                        raise ValueError(f'{field} points outside the isolated slot.')
                if record.get('status') == 'success':
                    result_path = Path(record['result_path'])
                    if not result_path.is_relative_to(cli_out) or not result_path.is_file():
                        raise ValueError('Successful official result is missing or comes from another slot.')
            if score(cli_summary['best_record']) > score(initial):
                raise ValueError('CLI best result regressed relative to its reevaluated input plan.')
            counts_complete = True
        except Exception as exc:
            error_type, error, error_traceback = type(exc).__name__, str(exc), traceback.format_exc()
    if error is not None:
        # Preserve observations without selecting the best partial attempt and
        # presenting it as a successfully completed search.
        calls = [dict(name='recovered_attempt', stage='initial' if record in initial_records else 'refinement',
                      record=record, accepted=None, route_order='completed_record_mtime') for record in attempts]

    statuses = Counter(call['record']['status'] for call in calls)
    best = cli_summary['best_record'] if counts_complete else None
    logical = cli_summary['logical_calls'] if counts_complete else None
    new = cli_summary['new_calls'] if counts_complete else None
    elapsed = cli_summary.get('elapsed_seconds') if counts_complete else None
    before = score(initial)[0] if initial and initial.get('status') == 'success' else None
    after = score(best)[0] if best else None
    stop_reason = cli_summary.get('stop_reason') if counts_complete else ('watchdog_timeout' if watchdog_timeout else 'exception')
    if counts_complete and stop_reason is None:
        stop_reason = 'initial_only_budget_or_time'
    detail = dict(case=case, problem=3, num_cores=5, method=method, seed=17,
                  status='success' if counts_complete else ('timeout' if watchdog_timeout else 'error'),
                  before=before, after=after, makespan=after,
                  budget=budget, seconds=seconds, logical_calls=logical, new_calls=new,
                  calls_complete=counts_complete, calls=calls,
                  observed_logical_calls=len(calls), observed_new_calls=sum(not call['record']['cache_hit'] for call in calls),
                  eval_failed_calls=sum(count for status, count in statuses.items() if status != 'success') if counts_complete else None,
                  eval_timeout_calls=statuses.get('timeout', 0) if counts_complete else None,
                  observed_eval_failed_calls=sum(count for status, count in statuses.items() if status != 'success'),
                  observed_eval_timeout_calls=statuses.get('timeout', 0), eval_status_counts=dict(statuses),
                  elapsed_seconds=elapsed, subprocess_wall_seconds=process_wall,
                  slot_wall_seconds=time.monotonic() - started,
                  stop_reason=stop_reason, returncode=returncode,
                  watchdog_timeout=watchdog_timeout, watchdog_seconds=watchdog_seconds,
                  input_plan=str(input_plan), winner_plan=best['plan_path'] if best else None,
                  official_result_path=best['result_path'] if best else None,
                  best_record=best, initial_record=initial, cli_summary_path=str(cli_out / 'summary.json'),
                  command=command, record_read_errors=record_errors,
                  error_type=error_type, error=error, traceback=error_traceback,
                  fresh_evaluations=True, historical_cache_reuse=False,
                  scope='Exported warm plan; initial observation reevaluated here; fresh isolated candidate directories; '
                        'within-slot duplicate cache reuse is counted and recorded; initial call included in total budget.')
    atomic_json(slot / 'summary.json', detail)
    row_fields = ('case', 'method', 'status', 'before', 'after', 'makespan', 'logical_calls', 'new_calls',
                  'calls_complete', 'observed_logical_calls', 'observed_new_calls',
                  'elapsed_seconds', 'subprocess_wall_seconds', 'slot_wall_seconds', 'stop_reason',
                  'eval_failed_calls', 'eval_timeout_calls', 'observed_eval_failed_calls',
                  'observed_eval_timeout_calls', 'returncode', 'watchdog_timeout',
                  'input_plan', 'winner_plan', 'official_result_path', 'error_type', 'error')
    row = {field: detail[field] for field in row_fields}
    row['summary_path'] = str(slot / 'summary.json')
    return row, detail


def main(args):
    plans, out = Path(args.plans).expanduser().resolve(), Path(args.out).expanduser().resolve()
    if out.exists():
        raise ValueError('Use a new output directory.')
    if out == DATA or DATA in out.parents:
        raise ValueError('Outputs must be outside official data.')
    if args.budget < 1 or args.seconds <= 0 or not math.isfinite(args.seconds):
        raise ValueError('Budget and seconds must be positive and finite.')
    inputs = {case: plans / '方案' / 'p3' / 'n5' / (case + '_multicore_res.json') for case in args.cases}
    for case, path in inputs.items():
        if not path.is_file():
            raise ValueError(f'Missing exported P3 five-core plan: {path}; --plans must be the export root.')
        plan = read_json(path)
        if set(plan) != {'node_to_subgraph', 'core_schedules'} or len(plan['core_schedules']) != 5:
            raise ValueError(f'Expected a two-field five-core plan: {path}')
    out.mkdir(parents=True, exist_ok=False)
    jobs = [(case, method) for case in args.cases for method in args.methods]
    random.Random(17).shuffle(jobs)
    atomic_json(out / 'plan.json', dict(plans=str(plans), input_plans={case: str(path) for case, path in inputs.items()},
                cases=args.cases, methods=args.methods, cores=5, seed=17,
                budget=args.budget, seconds=args.seconds, workers=1, serial=True, jobs=jobs,
                python=sys.executable, cli=str(CLI), watchdog_grace_seconds=WATCHDOG_GRACE_SECONDS,
                fresh_evaluations=True, initial_call_included=True,
                scope='Serial CLI measurements from exported warm plans, including a fresh initial official evaluation; '
                      'no historical evaluation reuse; within-slot duplicate caching remains visible.'))
    started = time.monotonic()
    rows, slots = [], []

    def save(complete=False, active=None):
        ordered = sorted(rows, key=lambda row: (row['case'], args.methods.index(row['method'])))
        counts_complete = all(row['calls_complete'] for row in rows)
        summary = dict(complete=complete, completed=len(rows), expected=len(jobs),
                       successful=sum(row['status'] == 'success' for row in rows),
                       failed=sum(row['status'] != 'success' for row in rows), active=active,
                       cases=args.cases, methods=args.methods, cores=5, seed=17,
                       budget=args.budget, seconds=args.seconds, serial=True,
                       wall_seconds=time.monotonic() - started,
                       sum_subprocess_wall_seconds=sum(row['subprocess_wall_seconds'] or 0 for row in rows),
                       logical_calls=sum(row['logical_calls'] for row in rows) if counts_complete else None,
                       new_calls=sum(row['new_calls'] for row in rows) if counts_complete else None,
                       observed_logical_calls=sum(row['observed_logical_calls'] for row in rows),
                       observed_new_calls=sum(row['observed_new_calls'] for row in rows),
                       rows=ordered, slots=slots,
                       scope='Sum of serial measurements; warm input plans but fresh initial observations and isolated search caches.')
        write_csv(out / 'results.csv', ordered)
        atomic_json(out / ('summary.json' if complete else 'progress.json'), summary)
        return summary

    save()
    for case, method in jobs:
        save(active=dict(case=case, method=method, slot=str(out / case / method)))
        row, detail = run_one(case, method, inputs[case], out / case / method, args.budget, args.seconds)
        rows.append(row)
        slots.append(detail)
        save()
        print(json.dumps(row, ensure_ascii=False), flush=True)
    result = save(complete=True)
    # Leave progress marked complete too, so a live viewer never appears stuck.
    atomic_json(out / 'progress.json', result)
    print(json.dumps(dict(complete=True, completed=result['completed'], failed=result['failed'],
                         wall_seconds=result['wall_seconds']), ensure_ascii=False), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plans', required=True, help='Export root containing 方案/p3/n5/*.json; not the n5 directory.')
    parser.add_argument('--cases', type=case_list, required=True)
    parser.add_argument('--methods', type=method_list, default=list(DEFAULT_METHODS))
    parser.add_argument('--out', required=True)
    parser.add_argument('--budget', type=int, default=13, help='Total calls, including the initial plan evaluation.')
    parser.add_argument('--seconds', type=float, default=120, help='Total soft solver seconds, including initial evaluation.')
    parsed = parser.parse_args()
    try:
        main(parsed)
    except ValueError as exc:
        parser.error(str(exc))
