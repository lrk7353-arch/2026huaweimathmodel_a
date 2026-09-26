"""Describe all independently replayed selected plans; never rank cold arms here."""
import argparse
import csv
import json
from pathlib import Path
from statistics import mean, median


def quantile(values, q):
    a = sorted(values)
    x = (len(a) - 1) * q
    lo = int(x)
    hi = min(lo + 1, len(a) - 1)
    return a[lo] + (a[hi] - a[lo]) * (x - lo)


def write_csv(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def analyze(source, out):
    raw = json.loads(source.read_text())
    assert raw['reconciliation']['all_verified']
    rows = []
    for r in raw['rows']:
        m = r['metrics']; d = m['data_movement_bytes']; c = m.get('cache_stats', {})
        cap = m['capacity_bytes']; peaks = m['memory_peak_by_core'].values()
        rows.append(dict(case=r['case'], problem=r['problem'], cores=r['cores'],
            makespan=m['makespan'], original_singlecore=r['original_singlecore'],
            speedup=r['original_singlecore']/m['makespan'],
            original_copy=d['original_graph_copy_bytes'], added_copy=d['added_copy_bytes'],
            partition_copy=d['partition_added_copy_bytes'], spill_copy=d['spill_added_copy_bytes'],
            copy_ratio=d['scheduled_copy_bytes']/max(1, d['original_graph_copy_bytes']),
            active_cores=m['active_cores'],
            l1_peak_ratio=max(v['L1'] for v in peaks)/cap['L1'],
            ub_peak_ratio=max(v['UB'] for v in peaks)/cap['UB'],
            cache_hit_bytes=c.get('hit_bytes', 0), cache_miss_bytes=c.get('miss_bytes', 0),
            cache_byte_hit_rate=c.get('hit_rate', 0),
            recovered_from_timeout=r['recovered_from_timeout'],
            evaluator_seconds=r['elapsed_seconds'], record_path=r['record_path']))
    by = {(r['case'], r['problem'], r['cores']): r for r in rows}
    expected = {(f'case_{i:03d}', p, n) for i in range(1, 101) for p in (1, 2, 3) for n in range(1, 6)}
    assert len(rows) == len(by) == 1500 and set(by) == expected
    assert all(r['partition_copy'] + r['spill_copy'] == r['added_copy'] for r in rows)
    groups = []
    for p in (1, 2, 3):
        for n in range(1, 6):
            g = [r for r in rows if r['problem'] == p and r['cores'] == n]
            s = [r['speedup'] for r in g]
            hits = sum(r['cache_hit_bytes'] for r in g)
            misses = sum(r['cache_miss_bytes'] for r in g)
            groups.append(dict(problem=p, cores=n, cases=len(g), mean_speedup=mean(s),
                median_speedup=median(s), q10_speedup=quantile(s, .1), min_speedup=min(s),
                max_speedup=max(s), below_core_count=sum(x<n for x in s),
                above_core_count=sum(x>n for x in s), below_original=sum(x<1 for x in s),
                makespan_sum=sum(r['makespan'] for r in g),
                mean_same_scene_1core_speedup=mean(by[r['case'],p,1]['makespan']/r['makespan'] for r in g),
                original_copy_sum=sum(r['original_copy'] for r in g),
                added_copy_sum=sum(r['added_copy'] for r in g),
                partition_copy_sum=sum(r['partition_copy'] for r in g),
                spill_copy_sum=sum(r['spill_copy'] for r in g),
                spill_cases=sum(r['spill_copy']>0 for r in g),
                copy_ratio_median=median(r['copy_ratio'] for r in g),
                copy_ratio_max=max(r['copy_ratio'] for r in g),
                fewer_active_cores=sum(r['active_cores']<n for r in g),
                l1_peak_ratio_max=max(r['l1_peak_ratio'] for r in g),
                ub_peak_ratio_max=max(r['ub_peak_ratio'] for r in g),
                cache_byte_hit_rate_mean=mean(r['cache_byte_hit_rate'] for r in g),
                cache_byte_hit_rate_weighted=hits/(hits+misses) if hits+misses else 0,
                cache_zero_hit_cases=sum(r['cache_hit_bytes']==0 for r in g)))
    regressions = []; scaling = []; worst = []
    for p in (1, 2, 3):
        g = [r for r in rows if r['problem']==p and r['cores']==5]
        worst.extend(sorted(g, key=lambda r:r['speedup'])[:10])
        for n in range(2, 6):
            comparisons = []
            for i in range(1, 101):
                case = f'case_{i:03d}'; a,b = by[case,p,n-1],by[case,p,n]
                reduction = 100*(1-b['makespan']/a['makespan'])
                comparisons.append(reduction)
                if reduction < 0:
                    regressions.append(dict(case=case,problem=p,cores=n,
                        previous_makespan=a['makespan'],makespan=b['makespan'],
                        slowdown_percent=-reduction,previous_copy=a['added_copy'],added_copy=b['added_copy']))
            scaling.append(dict(problem=p,cores=n,faster=sum(x>0 for x in comparisons),
                equal=sum(x==0 for x in comparisons),slower=sum(x<0 for x in comparisons),
                mean_time_reduction_percent=mean(comparisons),median_time_reduction_percent=median(comparisons)))
    p23 = []
    for i in range(1,101):
        case=f'case_{i:03d}'; a,b=by[case,2,5],by[case,3,5]
        p23.append(dict(case=case,p2_makespan=a['makespan'],p3_makespan=b['makespan'],
            time_reduction_percent=100*(1-b['makespan']/a['makespan']),
            p2_copy=a['added_copy'],p3_copy=b['added_copy'],cache_byte_hit_rate=b['cache_byte_hit_rate']))
    summary = dict(verified=1500, calls=raw['reconciliation']['original_calls']+raw['reconciliation']['additional_calls'],
        retrieved_at=raw['retrieved_at'], groups=groups, scaling=scaling, core_regressions=regressions,
        p23_n5=dict(faster=sum(r['time_reduction_percent']>0 for r in p23),
            equal=sum(r['time_reduction_percent']==0 for r in p23),slower=sum(r['time_reduction_percent']<0 for r in p23),
            mean_time_reduction_percent=mean(r['time_reduction_percent'] for r in p23)),
        worst_n5=worst, cold_snapshot=raw['cold_progress'],
        scope='Selected portfolio description; no global optimality claim, no equal-budget algorithm ranking, no official aggregate score')
    out.mkdir(parents=True, exist_ok=True)
    for name, data in [('1500复评明细',rows),('各场景各核数',groups),('增加核心对比',scaling),
                       ('增加核心退步项',regressions),('五核各场景最弱10项',worst),('P2P3五核逐图对比',p23)]:
        write_csv(out/(name+'.csv'),data)
    (out/'分析统计.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    return summary


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args(); s=analyze(a.source,a.out)
    print(json.dumps({k:v for k,v in s.items() if k not in ('worst_n5','core_regressions')},ensure_ascii=False,indent=2))
