"""Expose measured time/traffic/cache tradeoffs from already-paid cold calls.

These are optional operating points, not additional cold evaluations and not
changes to the official makespan-first default. All selected plans are bundled.
"""
import argparse
import csv
import gzip
import io
import json
from pathlib import Path
import tarfile

from common_run import atomic_json, read_json, score
from solver.common import object_digest


def hit_rate(r):
    c = r['metrics'].get('cache_stats') or {}
    return c.get('hit_bytes', 0)/max(1, c.get('hit_bytes', 0)+c.get('miss_bytes', 0))


def run(source, out):
    out.mkdir(parents=True, exist_ok=False)
    rows, records, keys = [], [], set()
    with gzip.open(source/'all_calls.jsonl.gz', 'rt', encoding='utf-8') as f, tarfile.open(out/'optional_plans.tar.gz', 'w:gz') as tar:
        for line in f:
            s = json.loads(line)
            if s['cores'] != 5 or s['problem'] not in (2, 3): continue
            key = s['case'], s['problem']; keys.add(key)
            unique = {c['record']['hashes']['plan_sha256']: c['record']
                      for c in s['calls'] if c['record']['status'] == 'success'}
            pool = list(unique.values()); fastest = min(pool, key=score)
            modes = {'time_first': fastest}
            for margin in (1, 3):
                allowed = [r for r in pool if score(r)[0]*100 <= score(fastest)[0]*(100+margin)]
                modes[f'min_copy_within_{margin}pct_time'] = min(allowed, key=lambda r: (score(r)[1], score(r)[0]))
            if s['problem'] == 3:
                allowed = [r for r in pool if score(r)[0]*100 <= score(fastest)[0]*101 and score(r)[1] <= score(fastest)[1]]
                modes['max_byte_hit_within_1pct_time_no_extra_copy'] = min(allowed, key=lambda r: (-hit_rate(r), score(r)))
            for name, r in modes.items():
                plan = read_json(r['plan_path'])
                if object_digest(plan) != r['hashes']['plan_sha256']: raise ValueError('source plan mismatch')
                member = f'p{s["problem"]}/{s["case"]}/{name}.json'
                payload = (json.dumps(plan, separators=(',', ':'))+'\n').encode()
                item = tarfile.TarInfo(member); item.size = len(payload); tar.addfile(item, io.BytesIO(payload))
                rows.append(dict(case=s['case'], problem=s['problem'], cores=5, mode=name,
                    makespan=score(r)[0], added_copy=score(r)[1],
                    byte_hit_rate=hit_rate(r) if s['problem'] == 3 else '',
                    time_ratio=score(r)[0]/score(fastest)[0],
                    copy_saved_vs_fastest=score(fastest)[1]-score(r)[1],
                    plan=member, plan_sha256=r['hashes']['plan_sha256']))
                records.append(dict(case=s['case'], problem=s['problem'], mode=name, record=r))
    if keys != {(f'case_{i:03d}', p) for i in range(1, 101) for p in (2, 3)}:
        raise ValueError('requires every five-core P2/P3 cold run')
    rows.sort(key=lambda r: (r['problem'], r['case'], r['mode']))
    with (out/'可选折中.csv').open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=rows[0]); w.writeheader(); w.writerows(rows)
    with gzip.open(out/'所选官方记录.json.gz', 'wt', encoding='utf-8') as f:
        json.dump(records, f, ensure_ascii=False, separators=(',', ':'))
    groups = []
    for p in (2, 3):
        for mode in sorted({r['mode'] for r in rows if r['problem'] == p}):
            subset = [r for r in rows if r['problem'] == p and r['mode'] == mode]
            groups.append(dict(problem=p, mode=mode, graphs=len(subset),
                copy_saved_bytes=sum(r['copy_saved_vs_fastest'] for r in subset),
                configurations_saving_copy=sum(r['copy_saved_vs_fastest'] > 0 for r in subset),
                max_time_ratio=max(r['time_ratio'] for r in subset),
                mean_graph_byte_hit_rate=sum(r['byte_hit_rate'] for r in subset)/len(subset) if p == 3 else None))
    atomic_json(out/'折中摘要.json', dict(complete=True, source=str(source), groups=groups,
        scope='post-hoc optional operating points from the same paid B24 pool; no new evaluations; not a cache causality claim or a changed default solver'))
    print(json.dumps(groups, ensure_ascii=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--source', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True); a = p.parse_args(); run(a.source, a.out)
