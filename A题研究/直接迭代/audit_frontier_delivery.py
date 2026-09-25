"""Audit the portable ledger and summarize selected time/traffic/cache metrics."""
import argparse
import csv
import math
from pathlib import Path

from common_run import DATA, GraphIR, read_json, atomic_json, validate_plan
from solver.common import object_digest

HERE=Path(__file__).resolve().parent


def main():
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path)
    p.add_argument('--baseline',type=Path,default=HERE/'三指标联合推进_20260926/累计1500配置成绩.csv')
    p.add_argument('--cache-baseline',type=Path,default=HERE/'三指标联合推进_20260926/交付100图P3五核三指标.csv')
    args=p.parse_args()
    out=args.directory
    oldrows=list(csv.DictReader(args.baseline.open(encoding='utf-8-sig')))
    rows=list(csv.DictReader((out/'累计1500配置成绩.csv').open(encoding='utf-8-sig')))
    key=lambda r:(r['case'],int(r['problem']),int(r['cores']))
    old={key(r):r for r in oldrows};graphs={};improved=[]
    expected={(f'case_{i:03d}',p,n) for i in range(1,101) for p in (1,2,3) for n in range(1,6)}
    assert len(rows)==1500 and {key(r) for r in rows}==expected
    for row in rows:
        k=key(row);before=old[k]
        if k[0] not in graphs:graphs[k[0]]=GraphIR.from_path(DATA/(k[0]+'.json'))
        plan=read_json(out/row['plan']);validate_plan(graphs[k[0]],plan)
        assert len(plan['core_schedules'])==k[2] and object_digest(plan)==row['plan_sha256'],k
        now=(int(row['makespan']),int(row['added_copy']))
        prior=(int(before['makespan']),int(before['added_copy']))
        assert now<=prior and int(row['original_singlecore'])==int(before['original_singlecore']),k
        assert math.isclose(float(row['speedup']),int(row['original_singlecore'])/now[0],rel_tol=1e-12),k
        if now<prior:improved.append(dict(case=k[0],problem=k[1],cores=k[2],before=prior,after=now))
    verification=read_json(out/'独立复评.json')['verified']
    assert len(verification)==len(improved)
    for v in verification:
        record=v['record'];k=(v['case'],v['problem'],v['cores'])
        row=next(r for r in rows if key(r)==k)
        assert record['status']=='success' and record['hashes']['plan_sha256']==row['plan_sha256']
    cache=list(csv.DictReader(args.cache_baseline.open(encoding='utf-8-sig')))
    updated={(v['case'],v['problem'],v['cores']):v['record'] for v in verification}
    for row in cache:
        r=updated.get((row['case'],3,5))
        if r:
            m=r['metrics'];c=m['cache_stats'];traffic=m['data_movement_bytes']
            hit,miss=c['hit_bytes'],c['miss_bytes']
            row.update(makespan=m['makespan'],copy=traffic['added_copy_bytes'],spill=traffic['spill_added_copy_bytes'],
                       hit_bytes=hit,miss_bytes=miss,hit_rate=hit/max(1,hit+miss),plan_sha256=r['hashes']['plan_sha256'])
    with (out/'P3五核三指标.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=cache[0]);w.writeheader();w.writerows(cache)
    means={p:dict(before=sum(float(r['speedup']) for r in oldrows if int(r['problem'])==p and int(r['cores'])==5)/100,
                  after=sum(float(r['speedup']) for r in rows if int(r['problem'])==p and int(r['cores'])==5)/100,
                  copy_before=sum(int(r['added_copy']) for r in oldrows if int(r['problem'])==p and int(r['cores'])==5),
                  copy_after=sum(int(r['added_copy']) for r in rows if int(r['problem'])==p and int(r['cores'])==5)) for p in (1,2,3)}
    stats=dict(mean_graph_byte_hit_rate=sum(float(r['hit_rate']) for r in cache)/100,
               weighted_byte_hit_rate=sum(int(r['hit_bytes']) for r in cache)/max(1,sum(int(r['hit_bytes'])+int(r['miss_bytes']) for r in cache)))
    result=dict(passed=True,plans=1500,exact_ordered_hashes=1500,regressions=0,improved_configurations=len(improved),
                independent_replays=len(verification),five_core=means,p3_cache=stats,improvements=improved,
                scope='portable plan legality, unchanged denominator, selected replay evidence; not cold performance')
    atomic_json(out/'交付审计.json',result)
    print({k:v for k,v in result.items() if k!='improvements'})


if __name__=='__main__':main()
