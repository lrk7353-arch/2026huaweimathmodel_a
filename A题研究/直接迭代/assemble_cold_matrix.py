"""Assemble portable cold runs without re-solving or importing warm starts.

Default requires the exact 100 graphs x 3 problems x 5 core counts. All paid
calls (including failures) remain in the portable archive. The original
single-core denominator is pinned by the supplied reference ledger.
"""
import argparse
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import statistics
import tarfile

from common_run import DATA, GraphIR, atomic_json, read_json, score, validate_plan
from solver.common import OFFICIAL, object_digest


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def key(row):
    return row['case'], int(row['problem']), int(row['cores'])


def write_csv(path, rows):
    with Path(path).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)


def assemble(exports, reference, out, allow_partial=False, prior_failures=()):
    out = Path(out)
    if out.exists():
        raise ValueError('output must be a new directory')
    denominator = {key(r): int(r['original_singlecore'])
                   for r in csv.DictReader(Path(reference).open(encoding='utf-8-sig'))}
    expected = {(f'case_{i:03d}', p, n) for i in range(1, 101)
                for p in (1, 2, 3) for n in range(1, 6)}
    index, manifests = {}, []
    for source in map(Path, exports):
        manifest = read_json(source/'export_manifest.json')
        rows = read_json(source/'selected_records.json')
        if not manifest['complete'] or manifest['configurations'] != len(rows):
            raise ValueError(('incomplete export', str(source)))
        manifests.append(dict(export=str(source), manifest=manifest,
            files={name: digest(source/name) for name in
                   ('plans.tar.gz', 'selected_records.json', 'all_calls.jsonl.gz', 'export_manifest.json')}))
        for row in rows:
            k = key(row)
            if k in index or k not in expected:
                raise ValueError(('duplicate or unexpected configuration', k))
            index[k] = (source, row)
    if not index or (set(index) != expected and not allow_partial):
        raise ValueError(('matrix is incomplete', len(index), 'expected', len(expected)))
    out.mkdir(parents=True)
    official = {p.name: digest(p) for p in OFFICIAL.glob('*.py')}
    config_hash = digest(DATA/'config.txt')
    graph_hash = {case: digest(DATA/(case+'.json')) for case in {k[0] for k in index}}

    def check_record(r, k):
        h = r['hashes']
        if (h['graph_sha256'] != graph_hash[k[0]] or h['config_sha256'] != config_hash
                or h['official_py_sha256'] != official or r['problem'] != k[1]):
            raise ValueError(('official input mismatch', k, r.get('attempt_id')))

    # Stream the immutable call ledgers rather than holding all attempts in RAM.
    accounted, attempts, call_count, failed_count = set(), set(), 0, 0
    selected_rows, metrics_rows = [], []
    with gzip.open(out/'all_calls.jsonl.gz', 'wt', encoding='utf-8') as dest:
        for source in map(Path, exports):
            with gzip.open(source/'all_calls.jsonl.gz', 'rt', encoding='utf-8') as f:
                for line in f:
                    s = json.loads(line); k = key(s)
                    if k in accounted or k not in index or index[k][0] != source:
                        raise ValueError(('call ledger key mismatch', k))
                    row = index[k][1]; calls = s['calls']; best = s['best_record']
                    if (not s['complete'] or s['budget'] != 24 or
                            len(calls) != s['logical_calls'] or len(calls) > 24 or
                            not best or best['status'] != 'success'):
                        raise ValueError(('invalid paid cold accounting', k))
                    if s.get('variant') not in ('mature', 'frontier'):
                        raise ValueError(('unexpected cold variant', k))
                    check_record(best, k)
                    successes = []
                    for c in calls:
                        r = c['record']; check_record(r, k)
                        attempts.add((r['attempt_id'], r['hashes']['plan_sha256'], k[1]))
                        if r['status'] == 'success': successes.append(r)
                    if (not successes or min(map(score, successes)) != score(best) or
                            not any(r['hashes']['plan_sha256'] == best['hashes']['plan_sha256']
                                    and score(r) == score(best) for r in successes)):
                        raise ValueError(('best is not a charged successful result', k))
                    if (row['logical_calls'] != len(calls) or row['best_record']['hashes'] != best['hashes']
                            or row['best_record']['metrics'] != best['metrics']):
                        raise ValueError(('selected record/accounting mismatch', k))
                    failures = len(calls)-len(successes)
                    call_count += len(calls); failed_count += failures
                    accounted.add(k); dest.write(json.dumps(s, ensure_ascii=False, separators=(',', ':'))+'\n')
                    member = f'plans/p{k[1]}/n{k[2]}/{k[0]}.json'
                    selected_rows.append(dict(row, plan_member=member))
                    m = best['metrics']; traffic = m['data_movement_bytes']; cache = m.get('cache_stats') or {}
                    hit, miss = cache.get('hit_bytes', 0), cache.get('miss_bytes', 0)
                    if m['num_cores'] != k[2]: raise ValueError(('core count mismatch', k))
                    metric = dict(case=k[0], problem=k[1], cores=k[2], variant=s['variant'],
                        original_singlecore=denominator[k], makespan=m['makespan'],
                        speedup=denominator[k]/m['makespan'], added_copy=traffic['added_copy_bytes'],
                        scheduled_copy=traffic['scheduled_copy_bytes'], spill=traffic['spill_added_copy_bytes'],
                        hit_bytes=hit, miss_bytes=miss, byte_hit_rate=hit/max(1, hit+miss) if k[1] == 3 else '',
                        calls=len(calls), failed_calls=failures, seconds=s['elapsed_seconds'],
                        generation_seconds=s.get('generation_seconds', 0),
                        generation_error_count=len(s.get('generation_errors', [])),
                        recovered_with_extended_time=bool(s.get('time_protocol')),
                        recovery_calls=s.get('time_protocol', {}).get('recovery_calls', 0),
                        recovery_elapsed_seconds=s.get('time_protocol', {}).get('recovery_elapsed_seconds', 0),
                        plan=member, plan_sha256=best['hashes']['plan_sha256'],
                        source_export=str(source), python=best['hashes'].get('python', '').split(' ')[0])
                    for b in (8, 12, 16, 24):
                        prefix = [c['record'] for c in calls[:b] if c['record']['status'] == 'success']
                        metric[f'B{b}_prefix_makespan'] = min(map(score, prefix))[0] if prefix else ''
                    metrics_rows.append(metric)
    if accounted != set(index): raise ValueError('missing call ledgers')

    # A fresh solve on another host is a paid retry, not a free replacement.
    # Preserve the failed first runs separately from the 1500 selected cold runs.
    prior_calls, prior_failed_calls, prior_seconds = 0, 0, 0.0
    retried_keys, prior_sources = set(), []
    if prior_failures:
        with gzip.open(out/'prior_failed_runs.jsonl.gz', 'wt', encoding='utf-8') as dest:
            for source in map(Path, prior_failures):
                prior_sources.append(dict(path=str(source),
                    manifest=read_json(source/'failed_runs_manifest.json'),
                    sha256=digest(source/'failed_runs.jsonl.gz')))
                with gzip.open(source/'failed_runs.jsonl.gz', 'rt', encoding='utf-8') as f:
                    for line in f:
                        s = json.loads(line); k = key(s); calls = s['calls']
                        if k not in index or s.get('best_record') or len(calls) != s['logical_calls']:
                            raise ValueError(('invalid prior failed run', k))
                        retried_keys.add(k); prior_calls += len(calls)
                        prior_seconds += s['elapsed_seconds']
                        for c in calls:
                            r = c['record']; check_record(r, k)
                            if r['status'] == 'success': raise ValueError(('successful run labeled failed', k))
                            prior_failed_calls += 1
                            attempts.add((r['attempt_id'], r['hashes']['plan_sha256'], k[1]))
                        dest.write(json.dumps(s, ensure_ascii=False, separators=(',', ':'))+'\n')

    # Decode one graph at a time; plan legality includes the quotient DAG.
    archives = {source: tarfile.open(source/'plans.tar.gz', 'r:gz') for source in map(Path, exports)}
    try:
        previous_case = None
        with tarfile.open(out/'plans.tar.gz', 'w:gz') as dest:
            for k, (source, row) in sorted(index.items()):
                if k[0] != previous_case:
                    ir = GraphIR.from_path(DATA/(k[0]+'.json')); previous_case = k[0]
                member = archives[source].getmember(row['plan_member'])
                if not member.isfile(): raise ValueError('plan archive entry must be a file')
                plan = json.load(archives[source].extractfile(member))
                if (object_digest(plan) != row['best_record']['hashes']['plan_sha256']
                        or len(plan['core_schedules']) != k[2]):
                    raise ValueError(('plan hash or core count mismatch', k))
                validate_plan(ir, plan)
                payload = (json.dumps(plan, ensure_ascii=False, separators=(',', ':'))+'\n').encode()
                entry = tarfile.TarInfo(f'plans/p{k[1]}/n{k[2]}/{k[0]}.json')
                entry.size = len(payload); dest.addfile(entry, io.BytesIO(payload))
    finally:
        for archive in archives.values(): archive.close()

    metrics_rows.sort(key=key); selected_rows.sort(key=key)
    write_csv(out/'从头1500配置成绩.csv' if not allow_partial else out/'从头部分配置成绩.csv', metrics_rows)
    atomic_json(out/'selected_records.json', selected_rows)
    groups = []
    for p in (1, 2, 3):
        for n in range(1, 6):
            rows = [r for r in metrics_rows if r['problem'] == p and r['cores'] == n]
            if not rows: continue
            groups.append(dict(problem=p, cores=n, graphs=len(rows),
                mean_speedup=statistics.mean(r['speedup'] for r in rows),
                added_copy_bytes=sum(r['added_copy'] for r in rows),
                spill_bytes=sum(r['spill'] for r in rows),
                calls=sum(r['calls'] for r in rows), failed_calls=sum(r['failed_calls'] for r in rows),
                recovered_configurations=sum(r['recovered_with_extended_time'] for r in rows),
                sum_solver_seconds=sum(r['seconds'] for r in rows),
                mean_graph_byte_hit_rate=statistics.mean(r['byte_hit_rate'] for r in rows) if p == 3 else '',
                weighted_byte_hit_rate=sum(r['hit_bytes'] for r in rows)/max(1, sum(r['hit_bytes']+r['miss_bytes'] for r in rows)) if p == 3 else ''))
    write_csv(out/'按问题与核数汇总.csv', groups)
    manifest = dict(complete=True, full_matrix=set(index) == expected, configurations=len(index),
        logical_calls=call_count, failed_calls=failed_count,
        unique_recorded_attempts_including_retries=len(attempts),
        retried_configurations=len(retried_keys), prior_failed_run_calls=prior_calls,
        prior_failed_calls=prior_failed_calls, prior_failed_run_seconds=prior_seconds,
        total_calls_including_retries=call_count+prior_calls,
        total_failed_calls_including_retries=failed_count+prior_failed_calls,
        extended_time_recoveries=sum(r['recovered_with_extended_time'] for r in metrics_rows),
        prior_failed_sources=prior_sources,
        failed_call_fraction=failed_count/max(1, call_count), reference_ledger=str(reference),
        reference_sha256=digest(reference), source_runs=manifests,
        official_py_sha256=official, config_sha256=config_hash,
        verified=dict(plan_legality=len(index), ordered_plan_hashes=len(index),
                      charged_best_records=len(index), unchanged_denominators=len(index)),
        scope='selected cold B24 runs; any failed initial runs and subsequent fresh retries are separately charged, not claimed as one successful B24 attempt; prefixes are prefixes of B24 trajectories, not separate B8/B12/B16 solves; no historical selected plan initializations; solver seconds are summed per-run wall time, not batch elapsed time',
        groups=groups)
    atomic_json(out/'export_manifest.json', manifest)
    print(json.dumps({k: manifest[k] for k in ('full_matrix', 'configurations', 'logical_calls', 'failed_calls', 'groups')}, ensure_ascii=False))


def plot(out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    groups = read_json(Path(out)/'export_manifest.json')['groups']
    fig, ax = plt.subplots(figsize=(7.2, 4.6), constrained_layout=True)
    for p in (1, 2, 3):
        rows = [r for r in groups if r['problem'] == p]
        if rows: ax.plot([r['cores'] for r in rows], [r['mean_speedup'] for r in rows], marker='o', label=f'P{p}')
    ax.set(xlabel='Number of cores', ylabel='Mean speedup (100 graphs per point)',
           title='Cold B24: 240 s primary + logged empty-run recovery', xticks=range(1, 6))
    ax.grid(alpha=.25); ax.legend()
    fig.savefig(Path(out)/'cold_speedup.png', dpi=180)
    fig.savefig(Path(out)/'cold_speedup.pdf'); plt.close(fig)


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--exports', type=Path, nargs='+', required=True)
    p.add_argument('--reference', type=Path, required=True); p.add_argument('--out', type=Path, required=True)
    p.add_argument('--prior-failures', type=Path, nargs='*', default=[])
    p.add_argument('--allow-partial', action='store_true'); a = p.parse_args()
    assemble(a.exports, a.reference, a.out, a.allow_partial, a.prior_failures)
    if not a.allow_partial: plot(a.out)
