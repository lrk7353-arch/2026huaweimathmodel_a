"""Archive completed paired runs and calculate budget-prefix comparisons."""
import argparse
import csv
import gzip
import json
import math
from pathlib import Path
import statistics

from common_run import atomic_json, read_json, score


def summarize(root, out):
    root, out = Path(root), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    ledger = Path(__file__).with_name('三指标联合推进_20260926')/'累计1500配置成绩.csv'
    denominator = {(r['case'], int(r['problem']), int(r['cores'])): int(r['original_singlecore'])
                   for r in csv.DictReader(ledger.open(encoding='utf-8-sig'))}
    summaries = [read_json(Path(r['summary'])) for r in read_json(root/'results.json')]
    assert all(s['complete'] and s['logical_calls'] <= s['budget'] for s in summaries)
    atomic_json(out/'manifest.json', read_json(root/'manifest.json'))
    with gzip.open(out/'全部调用.json.gz', 'wt', encoding='utf-8') as f:
        json.dump(summaries, f, ensure_ascii=False, separators=(',', ':'))
    rows = []
    for s in summaries:
        key = (s['case'], s['problem'], s['cores'])
        successful = [c for c in s['calls'] if c['record']['status'] == 'success']
        row = dict(case=key[0], problem=key[1], cores=key[2], variant=s['variant'],
                   makespan=score(s['best_record'])[0], copy=score(s['best_record'])[1],
                   speedup=denominator[key]/score(s['best_record'])[0], calls=s['logical_calls'],
                   failures=s['logical_calls']-len(successful), seconds=s['elapsed_seconds'],
                   generation_seconds=s['generation_seconds'])
        for b in (8, 12, 16, 24):
            prefix=[c['record'] for c in s['calls'][:b] if c['record']['status']=='success']
            row[f'B{b}_makespan'] = min(map(score,prefix))[0] if prefix else None
        rows.append(row)
    with (out/'逐配置.csv').open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    aggregate={}
    for problem in sorted({r['problem'] for r in rows}):
        pairs={}
        for r in rows:
            if r['problem']==problem:pairs.setdefault((r['case'],r['cores']),{})[r['variant']]=r
        matched=[v for v in pairs.values() if 'mature' in v and 'frontier' in v]
        metrics={}
        for variant in ('mature','frontier'):
            selected=[p[variant] for p in matched]
            metrics[variant]=dict(mean_speedup=statistics.mean(r['speedup'] for r in selected),
                calls=sum(r['calls'] for r in selected),failures=sum(r['failures'] for r in selected),
                sum_seconds=sum(r['seconds'] for r in selected),
                added_copy_bytes=sum(r['copy'] for r in selected),
                median_generation_seconds=statistics.median(r['generation_seconds'] for r in selected))
        ratios=[p['mature']['makespan']/p['frontier']['makespan'] for p in matched]
        aggregate[problem]=dict(pairs=len(matched),wins=sum(x>1 for x in ratios),
            ties=sum(x==1 for x in ratios),losses=sum(x<1 for x in ratios),
            geometric_speed_ratio=math.exp(statistics.mean(map(math.log,ratios))),metrics=metrics)
    atomic_json(out/'比较摘要.json',aggregate)
    print(json.dumps(aggregate,ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root');p.add_argument('out')
    a=p.parse_args();summarize(a.root,a.out)
