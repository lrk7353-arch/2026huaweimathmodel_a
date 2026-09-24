#!/usr/bin/env python3
"""Freeze completed formal P2/P3 selections and fill only their cross cells.

No optimization, no selected-plan feedback, no official/config/source mutations.
Logical cross-call cap is 800 across all attempts, including cache hits, failures
and interrupted reservations. Resume never silently repeats a failed call.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import argparse
import csv
import fcntl
import gzip
import json
import math
import os
from pathlib import Path
import statistics
import sys
import threading
import time
import traceback

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
sys.path[:0] = [str(RESEARCH / 'solver'), str(RESEARCH / 'advanced_solver')]
from common import OFFICIAL, atomic_json, digest, object_digest, read_json
from evaluator import evaluate
from graph_ir import GraphIR
from plan import validate_plan

FORMAL = RESEARCH / 'advanced_solver/runs/formal_v2'
DEFAULT_OUT = HERE / '正式P3四格_v2'
CASES = ['case_{:03d}'.format(i) for i in range(1, 101)]
CORES = [2, 3, 4, 5]
CROSS = {'t3_pi2': (3, 'pi2'), 't2_pi3': (2, 'pi3')}
CAP = 800


def require(condition, message):
    if not condition:
        raise ValueError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def check_hashes(hashes):
    for path, expected in hashes.items():
        require(Path(path).is_file() and digest(path) == expected, 'frozen file changed or missing: ' + path)


def add_hash(hashes, path):
    path = str(Path(path).resolve())
    value = digest(path)
    require(path not in hashes or hashes[path] == value, 'source changed during freeze: ' + path)
    hashes[path] = value
    return value


def timeout_for_ops(count):
    return 60.0 if count <= 10000 else 180.0


def validate_output_path(out, formal, *, resume):
    require(HERE.resolve() in out.parents, 'output must be a dedicated child of the experiment directory')
    require(out != formal and out not in formal.parents and formal not in out.parents, 'output overlaps formal source/cache tree')
    if resume:
        require((out / 'launch.json').is_file(), '--resume requires an existing matching launch')
    elif out.exists():
        require(out.is_dir() and not any(out.iterdir()), 'new run requires a new or empty directory; use --resume for an existing run')


def ratios(cells):
    if any(cells[k].get('status') != 'success' for k in ('t2_pi2', 't3_pi2', 't2_pi3', 't3_pi3')):
        return None
    a, b, c, d = [cells[k]['metrics']['makespan'] for k in ('t2_pi2', 't3_pi2', 't2_pi3', 't3_pi3')]
    require(all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (a, b, c, d)), 'invalid makespan')
    return {'hardware_on_pi2': a / b, 'policy_in_P3': b / d, 'total': a / d,
            'policy_in_P2': a / c, 'hardware_on_pi3': c / d}


def metrics_view(record):
    m = record.get('metrics', {})
    cache = m.get('cache_stats')
    amount = cache.get('hit_bytes', 0) + cache.get('miss_bytes', 0) if cache is not None else None
    return {'status': record.get('status'), 'makespan': m.get('makespan'),
            'data_movement_bytes': m.get('data_movement_bytes'), 'cache_stats': cache,
            'cache_byte_hit_rate': cache.get('hit_bytes', 0) / amount if amount else (0.0 if cache is not None else None),
            'evaluation_cache_hit': record.get('cache_hit'), 'result_path': record.get('result_path'),
            'result_sha256': record.get('result_sha256'), 'record_path': record.get('record_path'),
            'plan_sha256': record.get('hashes', {}).get('plan_sha256')}


def verify_record(record, *, problem, cores, graph_sha, config_sha, source_hashes, plan=None):
    require(record.get('status') == 'success', 'expected a successful official record')
    h = record['hashes']
    expected_official = {Path(p).name: v for p, v in source_hashes.items() if Path(p).parent.resolve() == OFFICIAL.resolve()}
    require(record.get('problem') == h.get('problem') == problem, 'record scene mismatch')
    require(h.get('graph_sha256') == graph_sha and h.get('config_sha256') == config_sha, 'record input mismatch')
    require(h.get('official_py_sha256') == expected_official, 'record official source mismatch')
    require(h.get('wrapper_sha256') == source_hashes[str((RESEARCH / 'solver/evaluator.py').resolve())], 'wrapper version mismatch')
    require(h.get('worker_sha256') == source_hashes[str((RESEARCH / 'solver/eval_worker.py').resolve())], 'worker version mismatch')
    require(Path(record['record_path']).is_file() and read_json(record['record_path']) == record, 'persisted record mismatch')
    if problem:
        require(plan is not None and object_digest(plan) == h.get('plan_sha256'), 'exact plan hash mismatch')
        require(digest(record['plan_path']) == h['plan_sha256'], 'worker input plan bytes mismatch')
        require(object_digest(read_json(record['plan_path'])) == h['plan_sha256'], 'worker parsed plan mismatch')
    require(digest(record['result_path']) == record['result_sha256'], 'official gzip hash mismatch')
    with gzip.open(record['result_path'], 'rt', encoding='utf-8') as handle:
        raw = json.load(handle)
    require(raw.get('num_cores') == cores == record['metrics'].get('num_cores'), 'core count mismatch')
    for field in ('makespan', 'data_movement_bytes', 'cache_stats'):
        require(raw.get(field) == record['metrics'].get(field), 'official raw metric mismatch: ' + field)
    if problem == 3:
        require(isinstance(raw.get('cache_stats'), dict), 'missing P3 cache metrics')
    return metrics_view(record)


def build_launch(formal, workers):
    mains = {str(p): formal / ('full_p{}_seed17'.format(p)) for p in (2, 3)}
    manifests = {p: read_json(d / 'manifest.json') for p, d in mains.items()}
    common = manifests['2']
    fixed = dict(common['source_sha256'])
    fixed[str((RESEARCH / 'advanced_solver/batch.py').resolve())] = common['batch_py_sha256']
    fixed[str(Path(__file__).resolve())] = digest(__file__)
    quick = dict(fixed)
    for p, m in manifests.items():
        s = m['settings']
        require(s['cases'] == list(range(1, 101)) and s['cores'] == CORES and s['problems'] == [int(p)], 'wrong formal scope')
        require(s['seed'] == 17 and s['profile'] == 'full' and not s['warm_p2'] and not s['exploratory'], 'wrong formal protocol')
        require(m['source_sha256'] == common['source_sha256'] and m['graphs_sha256'] == common['graphs_sha256'], 'formal input/source mismatch')
        require(m['batch_py_sha256'] == common['batch_py_sha256'], 'formal batch controller version mismatch')
        require(m['config_sha256'] == common['config_sha256'] and s['config'] == common['settings']['config'], 'formal config mismatch')
        require(s['data_dir'] == common['settings']['data_dir'], 'formal data directory mismatch')
        require(s.get('evaluation_dir') == str((formal / 'evaluations').resolve()), 'shared evaluation directory mismatch')
        require(m['python'] == sys.version and Path(m['python_executable']).resolve() == Path(sys.executable).resolve(), 'use the original bundled Python runtime')
        add_hash(fixed, mains[p] / 'manifest.json')
        add_hash(quick, mains[p] / 'manifest.json')
    fixed[common['settings']['config']] = common['config_sha256']
    quick[common['settings']['config']] = common['config_sha256']
    for case in CASES:
        fixed[str(Path(common['settings']['data_dir']) / (case + '.json'))] = common['graphs_sha256'][case]
    check_hashes(fixed)
    return {'schema_version': 1, 'formal_dir': str(formal), 'mains': {p: str(d) for p, d in mains.items()},
            'scope': {'cases': CASES, 'num_cores': CORES, 'pair_count': 400, 'N1_included': False},
            'logical_cross_call_cap_all_attempts': CAP, 'workers': workers, 'fixed_hashes': fixed,
            'quick_wait_hashes': quick, 'source_hashes': common['source_sha256'],
            'config': common['settings']['config'], 'config_sha256': common['config_sha256'],
            'data_dir': common['settings']['data_dir'], 'graphs_sha256': common['graphs_sha256'],
            'python': sys.version, 'python_executable': str(Path(sys.executable).resolve()),
            'timeout_rule': {'compute_ops_le_10000': 60.0, 'compute_ops_gt_10000': 180.0},
            'selection_feedback': False, 'baseline': str(RESEARCH / 'solver/runs/full_initial_v1/results')}


def main_status(launch):
    rows = {}
    for p, directory in launch['mains'].items():
        d = Path(directory)
        if not (d / 'summary.json').exists():
            progress = read_json(d / 'progress.json') if (d / 'progress.json').exists() else {}
            rows[p] = {'ready': False, 'state': 'awaiting_final_summary', 'reported_slots': progress.get('reported_slots', 0)}
            continue
        s = read_json(d / 'summary.json')
        require(s.get('source_hashes_unchanged') is True, 'main reported changed sources: ' + p)
        ready = (s.get('all_searches_completed') is True and s.get('completed_count') == 400
                 and s.get('reported_slots') == s.get('planned_slots') == 400)
        rows[p] = {'ready': ready, 'state': 'complete' if ready else 'main_incomplete',
                   'completed_count': s.get('completed_count'), 'feasible_count': s.get('feasible_count')}
    return rows


def wait_for_main(launch, out, *, wait, max_wait_seconds, interval):
    start = time.monotonic()
    while True:
        check_hashes(launch['quick_wait_hashes'])
        states = main_status(launch)
        atomic_json(out / 'dependency_progress.json', {'updated_at': now(), 'phase': 'waiting_for_main',
                    'dependencies': states, 'elapsed_seconds_this_wait': time.monotonic() - start,
                    'official_cross_calls_started': False})
        if all(s['ready'] for s in states.values()):
            return
        if not wait:
            raise RuntimeError('Formal P2/P3 are incomplete; --wait-for-main waits only for these two existing batches')
        if time.monotonic() - start >= max_wait_seconds:
            raise TimeoutError('Dependency wait limit reached; no cross evaluations launched')
        time.sleep(min(interval, max_wait_seconds - (time.monotonic() - start)))


def freeze_selection(launch, out):
    check_hashes(launch['fixed_hashes'])
    fixed, maps, manifests = dict(launch['fixed_hashes']), {}, {}
    for p, directory in launch['mains'].items():
        d = Path(directory)
        s = read_json(d / 'summary.json')
        require(s['all_searches_completed'] and s['source_hashes_unchanged'], 'main no longer ready')
        add_hash(fixed, d / 'summary.json')
        manifests[p] = read_json(d / 'manifest.json')
        indexed = {(r['case'], r['num_cores']): r for r in s['slots']}
        require(len(indexed) == len(s['slots']) == 400 and set(indexed) == {(c, n) for c in CASES for n in CORES}, 'duplicate or missing main slots')
        maps[p] = indexed
    selections = []
    for case in CASES:
        graph_path = Path(launch['data_dir']) / (case + '.json')
        graph_sha = launch['graphs_sha256'][case]
        ir = GraphIR.from_path(graph_path)
        baseline_path = Path(launch['baseline']) / case / 'singlecore.json'
        baseline = None
        baseline_error = None
        try:
            add_hash(fixed, baseline_path)
            baseline = read_json(baseline_path)
            verify_record(baseline, problem=0, cores=1, graph_sha=graph_sha, config_sha=launch['config_sha256'], source_hashes=launch['source_hashes'])
            for name in ('record_path', 'result_path'):
                add_hash(fixed, baseline[name])
        except Exception:
            baseline_error = traceback.format_exc()
            baseline = None
        for n in CORES:
            ident = '{}_n{}'.format(case, n)
            folder = out / 'inputs' / ident
            item = {'id': ident, 'case': case, 'num_cores': n, 'graph_path': str(graph_path), 'graph_sha256': graph_sha,
                    'compute_op_count': len(ir.compute_ids), 'timeout': timeout_for_ops(len(ir.compute_ids)),
                    'pi2': None, 'pi3': None, 'selection_errors': {}, 'singlecore': baseline, 'singlecore_error': baseline_error}
            for p in ('2', '3'):
                label = 'pi' + p
                row = maps[p][case, n]
                try:
                    require(row['problem'] == int(p) and row['search_completed'] is True, 'invalid formal slot')
                    add_hash(fixed, row['summary_path'])
                    s = read_json(row['summary_path'])
                    require(s.get('completed') is True and s.get('state') == 'finished', 'unfinished selected summary')
                    for k, value in [('graph_sha256', graph_sha), ('config_sha256', launch['config_sha256']),
                                     ('source_sha256', launch['source_hashes']), ('problem', int(p)), ('num_cores', n),
                                     ('profile', 'full'), ('seed', 17), ('initial_plan_sha256', None)]:
                        require(s.get(k) == value, 'selected summary mismatch: ' + k)
                    settings = manifests[p]['settings']
                    require(s['budgets'] == settings['budgets'] and s['total_evaluations'] == settings['max_evaluations'], 'budget mismatch')
                    require(s['max_rounds'] == settings['max_rounds'], 'round budget mismatch')
                    require(s['per_evaluation_timeout'] == (settings['timeout'] if settings['timeout'] is not None else item['timeout']), 'formal timeout mismatch')
                    require(s['evaluated_count'] == len(s['evaluations']) <= settings['max_evaluations'], 'evaluation count mismatch')
                    require(s.get('source_changed') is not True and s.get('source_hashes_unchanged') is not False, 'slot source mutation')
                    require(row['feasible'] and s['status'] == 'success' and s.get('best'), 'formal slot has no feasible plan')
                    best = s['best']
                    plan = read_json(row['plan_path'])
                    require(object_digest(plan) == object_digest(best['plan']) == best['plan_sha256'], 'published selected plan mismatch')
                    require(any(e['record'] == best['record'] for e in s['evaluations']), 'best absent from formal evaluation ledger')
                    verify_record(best['record'], problem=int(p), cores=n, graph_sha=graph_sha, config_sha=launch['config_sha256'], source_hashes=launch['source_hashes'], plan=plan)
                    require(row['makespan'] == best['record']['metrics']['makespan'], 'formal batch makespan mismatch')
                    validate_plan(ir, plan)
                    target = folder / (label + '.plan.json')
                    atomic_json(target, plan)
                    for source in (row['plan_path'], best['record']['record_path'], best['record']['plan_path'], best['record']['result_path'], target):
                        add_hash(fixed, source)
                    item[label] = {'plan_path': str(target), 'plan_sha256': object_digest(plan),
                                   'plan_file_sha256': digest(target), 'formal_summary_path': row['summary_path'],
                                   'formal_plan_path': row['plan_path'], 'stage': best.get('stage'),
                                   'name': best.get('name'), 'record': best['record']}
                except Exception:
                    item['selection_errors'][label] = traceback.format_exc()
            target = folder / 'selection.json'
            atomic_json(target, item)
            add_hash(fixed, target)
            selections.append({'id': ident, 'case': case, 'num_cores': n, 'path': str(target), 'sha256': digest(target)})
        atomic_json(out / 'freeze_progress.json', {'phase': 'freezing_selections_no_cross_calls',
                    'updated_at': now(), 'pairs_frozen': len(selections), 'planned_pairs': 400})
    check_hashes(fixed)
    manifest = {'schema_version': 1, 'frozen_at': now(), 'launch_sha256': digest(out / 'launch.json'),
                'scope': launch['scope'], 'fixed_hashes': fixed, 'selection': selections,
                'selection_rule': 'exact best from each completed formal P2/P3 slot; no cross-cell reselection',
                'baseline_rule': 'existing official problem=0 record only; no new baseline calls',
                'logical_cross_call_cap_all_attempts': CAP}
    atomic_json(out / 'selection_manifest.json', manifest)
    return manifest


def aggregate(rows, *, expected_cases=100, cores=CORES):
    result = {}
    for n in cores:
        selected = [r for r in rows if r['num_cores'] == n]
        complete = [r for r in selected if r['four_cell_complete']]
        all_complete = len(selected) == len(complete) == expected_cases and len({r['case'] for r in selected}) == expected_cases
        r = {'planned_cases': expected_cases, 'reported_cases': len(selected), 'complete_cases': len(complete),
             'all_cases_complete': all_complete, 'ratio_summary': None, 'official_mean_singlecore_over_T': None}
        if all_complete:
            r['ratio_summary'] = {key: {'arithmetic_mean': statistics.mean(x['ratios'][key] for x in complete),
                                       'geometric_mean': math.exp(statistics.mean(math.log(x['ratios'][key]) for x in complete)),
                                       'median': statistics.median(x['ratios'][key] for x in complete)} for key in complete[0]['ratios']}
            if all(x.get('singlecore_makespan') for x in complete):
                r['official_mean_singlecore_over_T'] = {k: statistics.mean(x['singlecore_makespan'] / x['metrics'][k]['makespan'] for x in complete)
                                                       for k in ('t2_pi2', 't3_pi2', 't2_pi3', 't3_pi3')}
        result[str(n)] = r
    return result


class Campaign:
    def __init__(self, launch, manifest, out, *, new_attempt=(), evaluator=evaluate):
        self.launch, self.manifest, self.out, self.evaluator = launch, manifest, out, evaluator
        self.new_attempt = set(new_attempt)
        self.lock = threading.RLock()
        self.entries = {}
        self.completed_rows = {}
        self.load_attempts()

    def load_attempts(self):
        for p in sorted((self.out / 'cross_calls').glob('*/*/attempt_*.json')):
            row = read_json(p)
            require(row['selection_manifest_sha256'] == digest(self.out / 'selection_manifest.json'), 'call belongs to another freeze')
            ident = row['cell_id']
            require(row['cell'] in CROSS and ident == row['pair_id'] + '/' + row['cell'], 'cross call identity mismatch')
            expected_path = self.out / 'cross_calls' / row['pair_id'] / row['cell'] / ('attempt_{:04d}.json'.format(row['attempt_number']))
            require(p.resolve() == expected_path.resolve() == Path(row['journal_path']).resolve(), 'journal path/attempt identity mismatch')
            require(type(row['attempt_number']) is int and row['attempt_number'] > 0, 'invalid attempt number')
            require(row['phase'] in ('reserved', 'interrupted', 'completed', 'controller_error'), 'invalid journal phase')
            require(row['phase'] != 'completed' or isinstance(row.get('record'), dict), 'completed journal missing record')
            require(row['problem'] == CROSS[row['cell']][0], 'cross call scene mismatch')
            item_path = next((x['path'] for x in self.manifest['selection'] if x['id'] == row['pair_id']), None)
            require(item_path is not None, 'unknown resumed pair')
            item = read_json(item_path)
            plan_meta = item[CROSS[row['cell']][1]]
            require(plan_meta is not None and row['plan_sha256'] == plan_meta['plan_sha256'], 'resumed cross plan mismatch')
            if row['phase'] == 'reserved':
                # Conservative crash accounting: the evaluator might have started.
                # Never blindly replay it or claim that no worker ran.
                row.update(phase='interrupted', finished_at=now(), error='Reserved before interruption; actual worker outcome not recovered. Retry requires --new-attempt and remaining global budget.')
                atomic_json(p, row)
            elif row.get('record'):
                record = row['record']
                if row.get('record_file_sha256'):
                    require(digest(record['record_path']) == row['record_file_sha256'], 'completed record changed')
                    require(read_json(record['record_path']) == record, 'completed record content changed')
                else:
                    require(row['phase'] == 'controller_error', 'completed record lacks persistence proof')
                if row['phase'] == 'completed' and record['status'] == 'success':
                    verify_record(record, problem=row['problem'], cores=item['num_cores'], graph_sha=item['graph_sha256'],
                                  config_sha=self.launch['config_sha256'], source_hashes=self.launch['source_hashes'], plan=read_json(plan_meta['plan_path']))
            self.entries.setdefault(ident, []).append(row)
        for ident, seq in self.entries.items():
            require([r['attempt_number'] for r in seq] == list(range(1, len(seq) + 1)), 'missing or reordered cell attempt: ' + ident)
        flat = [r for seq in self.entries.values() for r in seq]
        require(len(flat) <= CAP and len({r['logical_index'] for r in flat}) == len(flat), 'budget/duplicate index corruption')
        require(sorted(r['logical_index'] for r in flat) == list(range(1, len(flat) + 1)), 'missing logical reservation')
        for ident in self.new_attempt:
            require(ident in self.entries and not self.is_success(self.entries[ident][-1]), 'explicit retry must identify an existing failed cross cell: ' + ident)
        self.count = len(flat)

    @staticmethod
    def is_success(row):
        return row.get('phase') == 'completed' and row.get('record', {}).get('status') == 'success'

    @staticmethod
    def cell_record(row):
        if row['phase'] == 'completed':
            return row['record']
        return {'status': row['phase'], 'metrics': {}, 'error': row.get('error'),
                'untrusted_record': row.get('record')}

    def progress(self, phase='evaluating'):
        with self.lock:
            flat = [r for seq in self.entries.values() for r in seq]
            counts = Counter(r.get('record', {}).get('status', r['phase']) if r['phase'] == 'completed' else r['phase'] for r in flat)
            hits = sum(bool(r.get('record', {}).get('cache_hit')) for r in flat)
            atomic_json(self.out / 'progress.json', {'phase': phase, 'updated_at': now(), 'planned_pairs': 400,
                        'reported_pairs': len(self.completed_rows), 'complete_four_cells': sum(r['four_cell_complete'] for r in self.completed_rows.values()),
                        'logical_cross_calls_reserved': len(flat), 'logical_cap': CAP, 'status_counts': dict(counts),
                        'cache_hits': hits,
                        'uncached_recorded_calls': sum(r.get('record') is not None and not r['record'].get('cache_hit') for r in flat),
                        'confirmed_new_worker_calls': sum(r.get('record') is not None and not r['record'].get('cache_hit') and r['record'].get('returncode') is not None for r in flat),
                        'unresolved_calls': sum(r.get('record') is None for r in flat)})

    def cell(self, item, cell):
        problem, pi = CROSS[cell]
        selected = item[pi]
        if selected is None:
            return {'status': 'missing_formal_plan', 'metrics': {}, 'error': item['selection_errors'].get(pi)}
        ident = item['id'] + '/' + cell
        with self.lock:
            old = self.entries.get(ident, [])
            if old and (ident not in self.new_attempt or self.is_success(old[-1])):
                record = self.cell_record(old[-1])
                if record['status'] == 'success':
                    verify_record(record, problem=problem, cores=item['num_cores'], graph_sha=item['graph_sha256'],
                                  config_sha=self.launch['config_sha256'], source_hashes=self.launch['source_hashes'],
                                  plan=read_json(selected['plan_path']))
                return record
            if self.count >= CAP:
                return {'status': 'logical_budget_exhausted', 'metrics': {}, 'error': '800-call cap across all attempts'}
            self.new_attempt.discard(ident)
            self.count += 1
            number = len(old) + 1
            path = self.out / 'cross_calls' / item['id'] / cell / ('attempt_{:04d}.json'.format(number))
            row = {'logical_index': self.count, 'pair_id': item['id'], 'cell_id': ident, 'cell': cell,
                   'problem': problem, 'plan_sha256': selected['plan_sha256'], 'attempt_number': number,
                   'selection_manifest_sha256': digest(self.out / 'selection_manifest.json'),
                   'phase': 'reserved', 'reserved_at': now(), 'timeout_seconds': item['timeout'], 'journal_path': str(path)}
            atomic_json(path, row)  # Durable logical charge before any evaluator call.
            self.entries.setdefault(ident, []).append(row)
            self.progress()
        try:
            # Fail closed if a frozen plan/graph/config/evaluator changes mid-run.
            check_hashes(self.launch['quick_wait_hashes'])
            require(digest(item['graph_path']) == item['graph_sha256'], 'graph changed during cross measurements')
            require(digest(selected['plan_path']) == selected['plan_file_sha256'], 'frozen plan changed')
            plan = read_json(selected['plan_path'])
            record = self.evaluator(item['graph_path'], plan, problem, Path(self.launch['formal_dir']) / 'evaluations',
                                    timeout=item['timeout'], config_path=self.launch['config'], official_code=OFFICIAL)
            row.update(record=record, phase='controller_error', finished_at=now())
            atomic_json(path, row)  # Preserve an early wrapper failure even without record_path.
            row.update(record_file_sha256=digest(record['record_path']), phase='completed')
            atomic_json(path, row)  # Persist actual evaluator record even if validation below fails.
            if record['status'] == 'success':
                verify_record(record, problem=problem, cores=item['num_cores'], graph_sha=item['graph_sha256'],
                              config_sha=self.launch['config_sha256'], source_hashes=self.launch['source_hashes'], plan=plan)
        except Exception:
            row.update(phase='controller_error', error=traceback.format_exc(), finished_at=now())
            atomic_json(path, row)
            if row.get('record'):
                return {'status': 'record_verification_failure', 'metrics': {}, 'error': row['error'], 'untrusted_record': row['record']}
            return {'status': 'controller_error', 'metrics': {}, 'error': row['error']}
        finally:
            with self.lock:
                self.progress()
        return record

    def pair(self, ref):
        check_hashes(self.launch['quick_wait_hashes'])
        require(digest(ref['path']) == ref['sha256'], 'frozen selection changed')
        item = read_json(ref['path'])
        cells = {key: item[pi]['record'] if item[pi] else {'status': 'missing_formal_plan', 'metrics': {}, 'error': item['selection_errors'].get(pi)}
                 for key, pi in [('t2_pi2', 'pi2'), ('t3_pi3', 'pi3')]}
        for name in CROSS:
            cells[name] = self.cell(item, name)
            atomic_json(self.out / 'pairs' / item['id'] / 'progress.json', {'id': item['id'], 'cells': cells})
        if all(r.get('status') == 'success' for r in cells.values()):
            for a, b in [('t2_pi2', 't3_pi2'), ('t2_pi3', 't3_pi3')]:
                require(cells[a]['hashes']['plan_sha256'] == cells[b]['hashes']['plan_sha256'], 'four-cell plan mixing')
        ratio = ratios(cells)
        row = {'id': item['id'], 'case': item['case'], 'num_cores': item['num_cores'],
               'pi2_plan_sha256': item['pi2']['plan_sha256'] if item['pi2'] else None,
               'pi3_plan_sha256': item['pi3']['plan_sha256'] if item['pi3'] else None,
               'selection_errors': item['selection_errors'], 'four_cell_complete': ratio is not None,
               'singlecore_makespan': item['singlecore']['metrics']['makespan'] if item['singlecore'] else None,
               'singlecore_error': item['singlecore_error'], 'metrics': {k: metrics_view(v) for k, v in cells.items()},
               'cells': cells, 'ratios': ratio,
               'reuse_scope': 'two formal diagonal records cited; two logical cross calls include exact-cache reuse',
               'N1_included': False}
        atomic_json(self.out / 'pairs' / item['id'] / 'four_cells.json', row)
        with self.lock:
            self.completed_rows[item['id']] = row
            self.progress()
        return row

    def run(self):
        failures = []
        with ThreadPoolExecutor(max_workers=self.launch['workers']) as pool:
            pending = {pool.submit(self.pair, ref): ref for ref in self.manifest['selection']}
            for future in as_completed(pending):
                ref = pending[future]
                try:
                    future.result()
                except Exception:
                    error = {'id': ref['id'], 'case': ref['case'], 'num_cores': ref['num_cores'], 'four_cell_complete': False,
                             'error': traceback.format_exc(), 'ratios': None, 'metrics': {}}
                    atomic_json(self.out / 'pairs' / ref['id'] / 'controller_failure.json', error)
                    self.completed_rows[ref['id']] = error
                    failures.append(error)
        check_hashes(self.manifest['fixed_hashes'])
        rows = [self.completed_rows[x['id']] for x in self.manifest['selection']]
        flat = sorted([r for seq in self.entries.values() for r in seq], key=lambda r: r['logical_index'])
        counts = Counter(r.get('record', {}).get('status', r['phase']) if r['phase'] == 'completed' else r['phase'] for r in flat)
        summary = {'schema_version': 1, 'finished_at': now(), 'scope': self.launch['scope'], 'N1_status': 'not included; separate later supplement required',
                   'logical_cross_calls': len(flat), 'logical_cap': CAP, 'status_counts': dict(counts),
                   'cache_hits': sum(bool(r.get('record', {}).get('cache_hit')) for r in flat),
                   'uncached_recorded_calls': sum(r.get('record') is not None and not r['record'].get('cache_hit') for r in flat),
                   'confirmed_new_worker_calls': sum(r.get('record') is not None and not r['record'].get('cache_hit') and r['record'].get('returncode') is not None for r in flat),
                   'unresolved_calls': sum(r.get('record') is None for r in flat), 'pair_controller_failures': failures,
                   'four_cell_complete_count': sum(r['four_cell_complete'] for r in rows),
                   'all_four_cells_complete': len(rows) == 400 and all(r['four_cell_complete'] for r in rows),
                   'source_hashes_unchanged': True, 'selection_feedback': False,
                   'by_num_cores': aggregate(rows), 'pairs': [{k: v for k, v in r.items() if k != 'cells'} for r in rows],
                   'interpretation': 'H=T2(pi2)/T3(pi2); S=T3(pi2)/T3(pi3); R=T2(pi2)/T3(pi3). H*S=R per pair only. Conditional comparisons, not independent causal effects. Negative policy effects retained. Means suppressed unless all 100 complete at that N.'}
        atomic_json(self.out / 'summary.json', summary)
        columns = ['case', 'num_cores', 'four_cell_complete', 'singlecore_makespan', 'pi2_plan_sha256', 'pi3_plan_sha256']
        columns += [k + '_' + field for k in ('t2_pi2', 't3_pi2', 't2_pi3', 't3_pi3') for field in ('status', 'makespan', 'added_copy_bytes', 'cache_byte_hit_rate')]
        columns += ['hardware_on_pi2', 'policy_in_P3', 'total', 'policy_in_P2', 'hardware_on_pi3']
        with (self.out / 'four_cells.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                r = {k: row.get(k) for k in columns[:6]}
                for cell, m in row.get('metrics', {}).items():
                    r.update({cell + '_' + k: m.get(k) for k in ('status', 'makespan', 'cache_byte_hit_rate')})
                    r[cell + '_added_copy_bytes'] = (m.get('data_movement_bytes') or {}).get('added_copy_bytes')
                r.update(row.get('ratios') or {})
                writer.writerow(r)
        self.progress('finished')
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--formal-dir', type=Path, default=FORMAL)
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--workers', type=int, choices=(1, 2), default=1)
    parser.add_argument('--wait-for-main', action='store_true')
    parser.add_argument('--max-wait-hours', type=float, default=24.0)
    parser.add_argument('--wait-interval', type=float, default=30.0)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--new-attempt', action='append', default=[], metavar='CASE_N/CELL', help='Explicit failed cell ID, e.g. case_001_n2/t3_pi2; still inside total 800 cap')
    args = parser.parse_args(argv)
    require(math.isfinite(args.max_wait_hours) and args.max_wait_hours > 0 and 1 <= args.wait_interval <= 60, 'invalid finite wait settings')
    out = args.out.resolve()
    validate_output_path(out, args.formal_dir.resolve(), resume=args.resume)
    out.mkdir(parents=True, exist_ok=True)
    lock_handle = (out / '.controller.lock').open('a+')
    fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        expected = build_launch(args.formal_dir.resolve(), args.workers)
        launch_path = out / 'launch.json'
        if launch_path.exists():
            require(args.resume, 'existing launch requires --resume')
            launch = read_json(launch_path)
            require(launch == expected, 'launch/source/runtime/settings changed; cannot resume this freeze')
        else:
            require(not args.new_attempt, '--new-attempt needs an existing run')
            atomic_json(launch_path, expected)
            launch = expected
        manifest_path = out / 'selection_manifest.json'
        if manifest_path.exists():
            manifest = read_json(manifest_path)
            require(manifest['launch_sha256'] == digest(launch_path), 'launch hash changed')
            check_hashes(manifest['fixed_hashes'])
        else:
            wait_for_main(launch, out, wait=args.wait_for_main, max_wait_seconds=args.max_wait_hours * 3600, interval=args.wait_interval)
            manifest = freeze_selection(launch, out)
        campaign = Campaign(launch, manifest, out, new_attempt=args.new_attempt)
        summary = campaign.run()
        print(json.dumps({k: summary[k] for k in ('logical_cross_calls', 'cache_hits', 'four_cell_complete_count', 'all_four_cells_complete')}, ensure_ascii=False), flush=True)
        return 0 if summary['all_four_cells_complete'] else 2
    except Exception:
        atomic_json(out / 'controller_error.json', {'at': now(), 'error': traceback.format_exc(), 'no_automatic_retry': True})
        raise
    finally:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()


if __name__ == '__main__':
    raise SystemExit(main())
