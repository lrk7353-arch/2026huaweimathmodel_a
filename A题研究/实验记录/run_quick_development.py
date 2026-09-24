#!/usr/bin/env python3
"""One fixed 12-graph P2/N5/seed17 extra-budget development round, at most 108 calls."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import traceback
from types import SimpleNamespace

sys.dont_write_bytecode = True
RESEARCH = Path(__file__).resolve().parents[1]
LOGS = RESEARCH / '实验记录'
PANEL = LOGS / '快速迭代面板_v1/selection'
PORTFOLIO = RESEARCH / '当前最佳方案_v3_阶段快照'
PANEL_MANIFEST_SHA = 'a1a09633f2fb2a870aeaef1716a22155128137f1a10c90434c87597048951177'
sys.path.insert(0, str(RESEARCH / '精修求解器'))
import quick_refine
from controller import Solver, objective
from solver.common import DATA, atomic_json, digest, object_digest, read_json
from solver.graph_ir import GraphIR

SCOPE = ('fixed structural development panel; extra-budget warm-start search with private initially empty caches; '
         'not independent unseen data, not a fair-budget result or all100 efficacy/timing evidence')


def now():
    return datetime.now(timezone.utc).isoformat()


def require(value, message):
    if not value:
        raise ValueError(message)


def check_hashes(values):
    for path, expected in values.items():
        require(Path(path).is_file() and digest(path) == expected, 'frozen source/input/evidence changed: ' + path)


def contained(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    require(root in path.parents, 'evidence outside private run: ' + str(path))
    return path


def preflight(run_dir):
    """Read-only: validate fixed inputs and archived baseline raw evidence; never evaluate."""
    raw = Path(run_dir).expanduser()
    require(not raw.exists() and not raw.is_symlink(), 'run directory must be strictly new')
    out = raw.resolve()
    require(LOGS.resolve() in out.parents and not (PANEL.parent.resolve() in out.parents),
            'new run must be inside 实验记录 and outside the frozen panel folder')
    runtime = quick_refine.source_hashes()
    frozen = {**runtime, str(Path(__file__).resolve()): digest(__file__)}
    for path in (PANEL / 'manifest.json', PANEL / 'panels.json', PORTFOLIO / 'manifest.json', DATA / 'config.txt'):
        frozen[str(path)] = digest(path)
    require(digest(PANEL / 'manifest.json') == PANEL_MANIFEST_SHA, 'fixed panel identity changed')
    panel_manifest, panel = read_json(PANEL / 'manifest.json'), read_json(PANEL / 'panels.json')
    require(digest(PANEL / 'panels.json') == panel_manifest['outputs_sha256']['panels.json'], 'panel content checksum mismatch')
    cases = panel['development12']
    require(len(cases) == len(set(cases)) == 12, 'fixed development panel must have 12 unique graphs')
    portfolio = read_json(PORTFOLIO / 'manifest.json')
    require(portfolio['selected_count'] == 704 and not portfolio.get('rejected'), 'expected clean 704-plan frozen starting portfolio')
    jobs = []
    for index, case in enumerate(cases):
        graph = DATA / (case + '.json')
        plan_path = PORTFOLIO / 'p2/n5' / (case + '_multicore_res.json')
        provenance = plan_path.with_suffix('.provenance.json')
        record = read_json(provenance)['evaluation_record']
        plan = read_json(plan_path)
        graph_sha, plan_sha = digest(graph), object_digest(plan)
        require(graph_sha == panel_manifest['graphs_sha256'][case], 'panel graph hash mismatch: ' + case)
        require(record['hashes']['plan_sha256'] == plan_sha, 'incumbent differs from evaluated plan: ' + case)
        require(read_json(record['record_path']) == record, 'provenance and original record differ: ' + case)
        context = {'graph_sha256': graph_sha, 'config_sha256': digest(DATA / 'config.txt'), 'source_sha256': runtime}
        auditor = SimpleNamespace(manifest=context, ir=GraphIR.from_path(graph), graph_path=graph,
                                  num_cores=5, problem=2)
        Solver.verify_success(auditor, record, plan, plan_sha)
        for file in (graph, plan_path, provenance, Path(record['record_path']), Path(record['plan_path']), Path(record['result_path'])):
            frozen[str(file.resolve())] = digest(file)
        folder = out / case
        command = [sys.executable, '-B', str(RESEARCH / '精修求解器/quick_refine.py'), str(graph), '-n', '5', '-p', '2',
                   '--incumbent-plan', str(plan_path), '--config', str(DATA / 'config.txt'), '--run-dir', str(folder),
                   '--seed', '17', '--max-evaluations', '9', '--max-rounds', '2', '--round-width', '4',
                   '--trace-cap', '8', '--cache-cap', '0', '--time-budget', '120', '--timeout', '30']
        jobs.append({'index': index, 'case': case, 'problem': 2, 'num_cores': 5, 'seed': 17,
                     'graph_path': str(graph), 'graph_sha256': graph_sha, 'plan_path': str(plan_path),
                     'plan_file_sha256': digest(plan_path), 'plan_sha256': plan_sha,
                     'provenance_path': str(provenance), 'baseline_record_path': record['record_path'],
                     'baseline_result_path': record['result_path'], 'baseline_result_sha256': record['result_sha256'],
                     'baseline_objective': list(objective(record)), 'baseline_data_movement_bytes': record['metrics']['data_movement_bytes'],
                     'run_dir': str(folder), 'evaluation_dir': str(folder / 'evaluations'), 'command': command})
    check_hashes(frozen)
    return {'schema_version': 1, 'created_at': now(), 'scope': SCOPE, 'run_dir': str(out),
            'panel_manifest_sha256': PANEL_MANIFEST_SHA, 'jobs': jobs,
            'workers': 2, 'problem': 2, 'num_cores': 5, 'seed': 17, 'logical_cap_per_case': 9,
            'authorized_total_logical_cap': 108, 'time_budget_seconds_per_case': 120, 'time_budget_kind': 'soft',
            'per_evaluation_timeout_seconds': 30, 'empty_private_cache_per_case': True,
            'source_sha256': runtime, 'source_input_evidence_sha256': frozen,
            'python': sys.version, 'python_executable': str(Path(sys.executable).resolve()),
            'baseline_original_raw_verified_count': len(jobs), 'automatic_retry': False,
            'walltime_scope': 'observed concurrent wallclock including CLI startup and post-run audits; shares CPU with other batches; no timing extrapolation'}


def audit_case(job, manifest):
    """Verify all existing successful raw results, including attempts missing from a partial summary."""
    folder, evaluations = Path(job['run_dir']), Path(job['evaluation_dir'])
    context = {'graph_sha256': job['graph_sha256'], 'config_sha256': manifest['source_input_evidence_sha256'][str(DATA / 'config.txt')],
               'source_sha256': manifest['source_sha256']}
    auditor = SimpleNamespace(manifest=context, ir=GraphIR.from_path(job['graph_path']),
                              graph_path=Path(job['graph_path']), num_cores=5, problem=2)
    rows, attempts, errors = [], [], []
    trial_paths = sorted(p for p in (folder / 'trials').glob('*.json') if p.stem.isdigit())
    for p in trial_paths:
        try:
            row = read_json(p)
            require(row['index'] == len(rows) and p.stem == '{:04d}'.format(row['index']), 'trial index gap/mismatch')
            require(row['state'] in ('pending', 'returned'), 'unknown trial phase')
            plan_path = contained(row['plan_path'], folder / 'trials')
            require(digest(plan_path) == row['plan_file_sha256'], 'trial plan file hash mismatch')
            plan = read_json(plan_path)
            require(object_digest(plan) == row['plan_sha256'], 'trial plan content hash mismatch')
            if row.get('record', {}).get('status') == 'success':
                contained(row['record']['record_path'], evaluations)
                require(read_json(row['record']['record_path']) == row['record'], 'trial/original record mismatch')
                Solver.verify_success(auditor, row['record'], plan, row['plan_sha256'])
            rows.append(row)
        except Exception as exc:
            errors.append({'path': str(p), 'error': str(exc)})
    attempt_dirs = sorted(p for p in (evaluations / 'attempts').glob('*') if p.is_dir())
    raw_success_verified = 0
    for p in attempt_dirs:
        record_path = p / 'record.json'
        if not record_path.exists():
            attempts.append({'attempt_dir': str(p), 'status': 'missing_record', 'cache_hit': False})
            continue
        try:
            record = read_json(record_path)
            require(Path(record['record_path']).resolve() == record_path.resolve(), 'attempt record identity mismatch')
            if record.get('status') == 'success':
                raw_plan = contained(record['plan_path'], evaluations)
                contained(record['result_path'], evaluations)
                Solver.verify_success(auditor, record, read_json(raw_plan), record['hashes']['plan_sha256'])
                raw_success_verified += 1
            attempts.append({'attempt_dir': str(p), 'record_path': str(record_path), 'record_sha256': digest(record_path),
                             'status': record['status'], 'cache_hit': bool(record.get('cache_hit')),
                             'worker_returncode': record.get('returncode')})
        except Exception as exc:
            attempts.append({'attempt_dir': str(p), 'status': 'audit_failed', 'cache_hit': None, 'error': str(exc)})
            errors.append({'path': str(record_path), 'error': str(exc)})
    require(len(trial_paths) <= 9 and len(attempt_dirs) <= 9, 'per-case call/attempt cap exceeded')
    summary_path = folder / 'summary.json'
    summary = read_json(summary_path) if summary_path.exists() else None
    best = None
    try:
        require(len(attempts) == len(rows), 'trial/attempt count differs; preserve unresolved evidence')
        for row in rows:
            record = row.get('record', {})
            if row['state'] == 'returned' and record.get('status') == 'success':
                should_accept = best is None or objective(record) < objective(best['record'])
                require(row.get('accepted') == should_accept, 'objective acceptance mismatch')
                if should_accept:
                    best = row
            elif row.get('accepted'):
                raise ValueError('failed/pending trial accepted')
        if summary is not None:
            own_manifest = read_json(folder / 'manifest.json')
            require(all(summary.get(k) == v for k, v in own_manifest.items()), 'summary differs from own frozen manifest')
            require(summary['source_sha256'] == manifest['source_sha256'] and not summary.get('test_hooks_used'), 'source/test-hook mismatch')
            expected = {'problem': 2, 'num_cores': 5, 'seed': 17, 'max_evaluations': 9, 'max_rounds': 2,
                        'round_width': 4, 'timeout': 30, 'time_budget_seconds': 120,
                        'graph_sha256': job['graph_sha256'], 'incumbent_plan_sha256': job['plan_sha256'],
                        'incumbent_file_sha256': job['plan_file_sha256'], 'evaluation_dir': job['evaluation_dir'],
                        'external_evaluation_cache': False}
            require(all(summary.get(k) == v for k, v in expected.items()), 'quick settings/input mismatch')
            require(summary['logical_calls'] == len(rows) and summary['evaluations'] == rows, 'summary/trial ledger mismatch')
            require(all(r['state'] == 'returned' for r in rows), 'pending official trial retained')
            require(summary['status_counts'] == dict(Counter(r['record']['status'] for r in rows)), 'status accounting mismatch')
            require(summary['cache_hits'] == sum(bool(r['record'].get('cache_hit')) for r in rows), 'cache accounting mismatch')
            if best:
                stored = summary['best']
                require(stored['record'] == best['record'] and stored['plan_sha256'] == best['plan_sha256'], 'summary best differs from trial objective minimum')
                output = contained(summary['output'], folder)
                require(digest(output) == summary['output_sha256'] and object_digest(read_json(output)) == best['plan_sha256'], 'published best differs')
            else:
                require(not summary.get('best'), 'summary invents best')
        else:
            errors.append({'path': str(summary_path), 'error': 'missing summary; any surviving verified best is partial'})
    except Exception as exc:
        errors.append({'path': str(summary_path), 'error': str(exc)})
    initial = rows[0] if rows else None
    reproduced = bool(initial and initial['stage'] == 'initial' and initial['plan_sha256'] == job['plan_sha256']
                      and initial.get('record', {}).get('status') == 'success'
                      and list(objective(initial['record'])) == job['baseline_objective'])
    final_objective = list(objective(best['record'])) if best else None
    not_worse = bool(reproduced and final_objective is not None and tuple(final_objective) <= tuple(job['baseline_objective']))
    cache_hits = sum(bool(a.get('cache_hit')) for a in attempts)
    if cache_hits:
        errors.append({'path': str(evaluations), 'error': 'unexpected cache hit in private fresh exact-deduplicated run'})
    return {'logical_calls': len(trial_paths), 'verified_trial_ledger_rows': len(rows), 'attempts_count': len(attempts),
            'unresolved_attempts': sum(a['status'] == 'missing_record' for a in attempts),
            'pending_trials': sum(r['state'] != 'returned' for r in rows),
            'cache_hits': cache_hits, 'confirmed_worker_completions': sum(a.get('worker_returncode') is not None for a in attempts),
            'status_counts': dict(Counter(a['status'] for a in attempts)), 'attempt_evidence': attempts,
            'raw_success_verified': raw_success_verified, 'audit_errors': errors,
            'summary_path': str(summary_path), 'summary_sha256': digest(summary_path) if summary else None,
            'profile_completed': bool(summary and summary.get('profile_completed')),
            'partial_result': bool(summary and summary.get('partial_result')),
            'stop_reason': summary.get('stop_reason') if summary else 'missing_summary',
            'requires_review': bool(errors or (summary and summary.get('requires_review'))),
            'best_official_verified': bool(best and not errors),
            'original_incumbent_reproduced': reproduced, 'objective_not_worse': not_worse,
            'baseline_objective': job['baseline_objective'], 'final_objective': final_objective,
            'best_plan_path': best['plan_path'] if best else None,
            'best_plan_sha256': best['plan_sha256'] if best else None,
            'makespan_speedup_from_incumbent': job['baseline_objective'][0] / final_objective[0] if final_objective and reproduced and not errors else None}


def run(manifest, stop=None):
    stop = stop or threading.Event()
    out = Path(manifest['run_dir'])
    check_hashes(manifest['source_input_evidence_sha256'])
    out.mkdir(parents=True, exist_ok=False)
    atomic_json(out / 'manifest.json', manifest)
    started, results = time.perf_counter(), {}

    def job_run(job):
        row = {'case': job['case'], 'problem': 2, 'num_cores': 5, 'state': 'not_started', 'logical_calls': 0,
               'baseline_objective': job['baseline_objective'], 'created_at': now()}
        begin = time.perf_counter()
        try:
            if stop.is_set():
                row['stop_reason'] = 'stop_requested_before_dispatch'
                return row
            check_hashes(manifest['source_input_evidence_sha256'])
            require(not Path(job['run_dir']).exists(), 'case run/cache no longer fresh')
            atomic_json(out / (job['case'] + '.pending.json'), dict(job, reserved_at=now(), authorized_max_calls=9))
            row['state'] = 'dispatched'
            with (out / (job['case'] + '.stdout.log')).open('wb') as stdout, (out / (job['case'] + '.stderr.log')).open('wb') as stderr:
                process = subprocess.Popen(job['command'], stdout=stdout, stderr=stderr, start_new_session=True,
                                           env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
                atomic_json(out / (job['case'] + '.process.json'), {'pid': process.pid, 'parent_pid': os.getpid(), 'started_at': now()})
                row['exit_code'] = process.wait()  # Soft child budget; never kill a running official worker here.
            row.update(audit_case(job, manifest))
            check_hashes(manifest['source_input_evidence_sha256'])
            complete = (row['exit_code'] == 0 and row['profile_completed'] and row['best_official_verified']
                        and not row['requires_review'] and row['original_incumbent_reproduced'] and row['objective_not_worse'])
            row['state'] = 'completed_verified' if complete else ('partial_verified' if row['best_official_verified'] and row['objective_not_worse'] else 'needs_review')
        except Exception:
            row.update(state='controller_error', error=traceback.format_exc())
            stop.set()  # Drain the already-running other case; never dispatch further work after integrity/controller failure.
            try:
                row.update(audit_case(job, manifest))
            except Exception:
                row['audit_exception'] = traceback.format_exc()
        finally:
            row['elapsed_including_cli_and_audit_seconds'] = time.perf_counter() - begin
            row['finished_at'] = now()
            atomic_json(out / (job['case'] + '.result.json'), row)
        return row

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(job_run, job): job['case'] for job in manifest['jobs']}
        for future in as_completed(futures):
            row = future.result(); results[row['case']] = row
            atomic_json(out / 'progress.json', {'scope': SCOPE, 'updated_at': now(), 'reported_cases': len(results),
                        'planned_cases': 12, 'stop_requested': stop.is_set(),
                        'logical_calls': sum(r.get('logical_calls', 0) for r in results.values()),
                        'results': [results[j['case']] for j in manifest['jobs'] if j['case'] in results]})
    rows = [results[j['case']] for j in manifest['jobs']]
    unchanged = True
    try:
        check_hashes(manifest['source_input_evidence_sha256'])
    except Exception:
        unchanged = False
    calls = sum(r.get('logical_calls', 0) for r in rows)
    complete = unchanged and calls <= 108 and all(r['state'] == 'completed_verified' for r in rows)
    result = {'schema_version': 1, 'scope': SCOPE, 'finished_at': now(),
              'observed_wall_seconds_including_cli_and_audits': time.perf_counter() - started,
              'workers': 2, 'logical_call_cap': 108, 'logical_calls': calls,
              'source_input_evidence_hashes_unchanged': unchanged, 'stop_requested': stop.is_set(),
              'case_state_counts': dict(Counter(r['state'] for r in rows)), 'results': rows,
              'all_12_profiles_completed_and_verified': complete, 'all100_validation_done': False,
              'official_mean_speedup': None, 'automatic_retry': False}
    eligible = [r for r in rows if r.get('makespan_speedup_from_incumbent') is not None and r.get('objective_not_worse')]
    result['verified_gain_cases_count'] = len(eligible)
    result['strict_makespan_improvement_count'] = sum(r['makespan_speedup_from_incumbent'] > 1 for r in eligible)
    result['objective_improvement_count'] = sum(tuple(r['final_objective']) < tuple(r['baseline_objective']) for r in eligible)
    # A panel aggregate is reported only when the complete fixed panel verified.
    result['development_panel_mean_incumbent_over_final_time'] = sum(r['makespan_speedup_from_incumbent'] for r in eligible) / 12 if complete else None
    atomic_json(out / 'summary.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True, help='Strictly new directory under A题研究/实验记录')
    args = parser.parse_args(argv)
    stop = threading.Event()
    # A requested stop drains current children and preserves their full evidence.
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        result = run(preflight(args.run_dir), stop)
        print(json.dumps({k: result[k] for k in ('all_12_profiles_completed_and_verified', 'logical_calls',
            'observed_wall_seconds_including_cli_and_audits', 'case_state_counts', 'strict_makespan_improvement_count')}, ensure_ascii=False))
        return 0 if result['all_12_profiles_completed_and_verified'] else 1
    except Exception:
        print(json.dumps({'status': 'failed', 'error': traceback.format_exc()}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
