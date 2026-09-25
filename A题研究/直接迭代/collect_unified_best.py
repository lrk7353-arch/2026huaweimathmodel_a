"""Separate best-known library: collect actual wins, independently replay them.

Previously reported archive entries stay labelled historical, not freshly verified.
Every promoted new plan must pass an independent official call in this output.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import json
from pathlib import Path
import shutil
import statistics

from common_run import DATA, R, GraphIR, atomic_json, read_json, write_csv, evaluate
from audit_unified_results import sha, validate_record
from export_unified_campaign import read_csv


def collect(archive, roots, out, workers=4):
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    old = {(r['case'], int(r['problem']), int(r['cores'])): r for r in read_csv(archive)}
    assert len(old) == 1500
    candidates, attempts, unique = {}, [], set()
    for root in roots:
        for path in root.resolve().rglob('summary.json'):
            if 'source_snapshot' in path.parts:
                continue
            s = read_json(path)
            # Charge failed slots and original-single-core calls too, even
            # though they cannot contribute a winning candidate.
            for a in s.get('evaluations', []):
                r = a['record']
                if r['record_path'] not in unique:
                    unique.add(r['record_path'])
                    attempts.append(r)
            rec = s.get('best_record')
            if not rec or rec['status'] != 'success' or rec['problem'] not in (1, 2, 3):
                continue
            case = Path(rec['graph_path']).stem
            k = case, rec['problem'], rec['metrics']['num_cores']
            if k not in old:
                continue
            score = rec['metrics']['makespan'], rec['metrics']['data_movement_bytes']['added_copy_bytes']
            prior = float(old[k]['makespan']), float(old[k]['added_copy'])
            if score < prior and (k not in candidates or score < candidates[k][0]):
                candidates[k] = score, rec, path
    def replay(item):
        key, (_, rec, summary) = item
        identity = '_'.join(map(str,key))+'_'+rec['hashes']['plan_sha256'][:16]
        checkpoint = out/'复评记录'/(identity+'.json')
        if checkpoint.exists():
            verified = read_json(checkpoint)
            validate_record(verified, raw=True)
            cached = True
        else:
            plan = read_json(rec['plan_path'])
            ir = GraphIR.from_path(DATA/(key[0]+'.json'))
            validate_record(rec, ir, raw=True)
            verified = evaluate(DATA/(key[0]+'.json'), plan, key[1], out/'复评运行'/identity,
                timeout=180, config_path=DATA/'config.txt')
            atomic_json(checkpoint, verified)
            assert verified['status'] == 'success', (key, verified.get('error'))
            cached = False
        assert verified['metrics']['makespan'] == rec['metrics']['makespan'], key
        assert verified['hashes']['plan_sha256'] == rec['hashes']['plan_sha256'], key
        assert verified['metrics']['data_movement_bytes'] == rec['metrics']['data_movement_bytes'], key
        if key[1] == 3:
            assert verified['metrics']['cache_stats'] == rec['metrics']['cache_stats'], key
        return key, rec, verified, summary, cached
    winners, errors, fresh_calls, bundles = [], [], 0, {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(replay, item): item[0] for item in candidates.items()}
        for f in as_completed(jobs):
            try:
                key, rec, verified, source, cached = f.result()
                fresh_calls += not cached and not verified['cache_hit']
                rel = Path('新增精选方案')/f'p{key[1]}'/f'n{key[2]}'/(key[0]+'_multicore_res.json')
                target = out/rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(verified['plan_path'], target)
                bundles[str(rel)] = read_json(target)
                base = old[key]
                winners.append(dict(case=key[0], problem=key[1], cores=key[2],
                    before=int(base['makespan']), after=rec['metrics']['makespan'],
                    reduction=1-rec['metrics']['makespan']/int(base['makespan']),
                    before_added_copy=int(base['added_copy']), after_added_copy=rec['metrics']['data_movement_bytes']['added_copy_bytes'],
                    source_summary=str(source), plan=str(rel), plan_sha256=verified['hashes']['plan_sha256'],
                    replay_record=verified['record_path'], replay_reused=cached,
                    cache_byte_hit_rate=verified['metrics'].get('cache_stats', {}).get('hit_rate', '')))
                print(json.dumps(winners[-1], ensure_ascii=False), flush=True)
            except Exception as error:
                errors.append(dict(key=jobs[f], error=repr(error)))
    winner_map = {(r['case'],r['problem'],r['cores']):r for r in winners}
    cumulative = []
    for k, original in sorted(old.items()):
        r = dict(original)
        r['verification'] = 'historical_ledger'
        r['plan_base'] = 'repository'
        if k in winner_map:
            w = winner_map[k]
            r.update(makespan=w['after'], added_copy=w['after_added_copy'],
                plan=w['plan'], plan_base='delivery', source='unified_v2_best_known_replayed', verification='independent_official_replay',
                speedup=float(r['original_singlecore'])/w['after'])
        cumulative.append(r)
    gains = []
    for p in (1,2,3):
        for n in range(1,6):
            before = [float(v['speedup']) for k,v in old.items() if k[1:] == (p,n)]
            after = [float(v['speedup']) for v in cumulative if int(v['problem'])==p and int(v['cores'])==n]
            gains.append(dict(problem=p, cores=n, graphs=len(after), before_mean=statistics.mean(before),
                after_mean=statistics.mean(after), delta_mean=statistics.mean(after)-statistics.mean(before),
                improved=sum(w['problem']==p and w['cores']==n and w['after']<w['before'] for w in winners)))
    write_csv(out/'新增精选方案成绩.csv', sorted(winners, key=lambda r:(r['case'],r['problem'],r['cores'])))
    write_csv(out/'累计1500配置成绩.csv', cumulative)
    write_csv(out/'累计平均加速比变化.csv', gains)
    with (out/'新增精选方案集.json.gz').open('wb') as file, gzip.GzipFile(fileobj=file, mode='wb', mtime=0) as stream:
        stream.write(json.dumps(bundles, ensure_ascii=False, separators=(',',':')).encode())
    audit = dict(candidates=len(candidates), replayed=len(winners), errors=errors,
        new_replay_calls_this_run=fresh_calls, selected_replay_calls=len(winners),
        upstream_unique_official_calls=sum(not r['cache_hit'] for r in attempts),
        upstream_failures=sum(r['status']!='success' for r in attempts),
        all_replay_new_calls=sum(not read_json(p)['cache_hit'] for p in (out/'复评记录').glob('*.json')),
        interpretation='Cumulative best-known across explicitly listed development/full runs. Not a frozen from-scratch algorithm score. Historical unchanged rows are not freshly replayed.',
        source_archive=str(archive.resolve()), source_archive_sha256=sha(archive), run_roots=[str(r.resolve()) for r in roots])
    atomic_json(out/'精选库核验.json', audit)
    assert not errors, errors
    return audit


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive', type=Path, required=True)
    p.add_argument('--runs', type=Path, nargs='+', required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--workers', type=int, default=4)
    a = p.parse_args()
    print(json.dumps(collect(a.archive, a.runs, a.out, a.workers), ensure_ascii=False))
