"""Audit frozen warm results, register promotion decisions, save compact evidence."""
import csv
import gzip
import json
import math
from pathlib import Path
import statistics
import sys

HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parent))
from common_run import read_json,atomic_json,write_csv,score


def main():
    execution=read_json(HERE/'集中对照/execution.json');assert execution['complete']
    rows=list(csv.DictReader((HERE/'集中对照/逐臂结果.csv').open(encoding='utf-8-sig')))
    for r in rows:
        for k in ('problem','cores','makespan','added_copy','spill','calls','new_calls','failures','generation_failures'):r[k]=int(r[k])
        for k in ('generation_seconds','elapsed_seconds'):r[k]=float(r[k])
    results=[];calls=[];generations=[];pairs=[];winners=[]
    for p in (1,2,3):
        rs=[r for r in rows if r['problem']==p];lookup={(r['case'],r['policy']):r for r in rs}
        baseline=[r for r in rs if r['policy']=='mature'];joint=[r for r in rs if r['policy']=='wait_joint']
        ratios=[];wins=losses=ties=regress=0
        for r in sorted(joint,key=lambda r:r['case']):
            b=lookup[r['case'],'mature'];pr=lookup[r['case'],'proxy'];ratio=b['makespan']/r['makespan'];ratios.append(ratio)
            wins+=r['makespan']<b['makespan'];losses+=r['makespan']>b['makespan'];ties+=r['makespan']==b['makespan']
            regress+=r['makespan']>1.01*b['makespan']
            pairs.append(dict(case=r['case'],problem=p,mature=b['makespan'],proxy=pr['makespan'],wait_joint=r['makespan'],
                joint_time_reduction_vs_mature_pct=100*(1-r['makespan']/b['makespan']),
                mature_copy=b['added_copy'],joint_copy=r['added_copy'],joint_hit=r.get('hit_rate'),
                mature_seconds=b['elapsed_seconds'],joint_seconds=r['elapsed_seconds']))
        gain=math.exp(statistics.mean(math.log(x) for x in ratios))-1
        failure=sum(r['failures'] for r in joint)/max(1,sum(r['calls'] for r in joint))
        wall=statistics.median(r['elapsed_seconds'] for r in joint);oldwall=statistics.median(r['elapsed_seconds'] for r in baseline)
        gate=dict(geomean_speed_gain=gain,wins=wins,losses=losses,ties=ties,regress_over1pct=regress,
            evaluation_failure_rate=failure,generation_failures=sum(r['generation_failures'] for r in joint),
            median_seconds=wall,mature_median_seconds=oldwall)
        gate['passed']=gain>=.003 and wins>=2 and regress<=1 and failure<=.05 and wall<=2*oldwall+5
        results.append(dict(problem=p,**gate))
    for row in rows:
        s=read_json(row['summary']);initial=s['calls'][0]['record']
        for i,c in enumerate(s['calls']):calls.append(dict(case=row['case'],problem=row['problem'],policy=row['policy'],call=i+1,**c))
        generations.extend(dict(case=row['case'],problem=row['problem'],policy=row['policy'],**g) for g in s['generations'])
        if score(s['best_record'])<score(initial):winners.append(dict(case=row['case'],problem=row['problem'],policy=row['policy'],
            before=score(initial),after=score(s['best_record']),record=s['best_record'],initial_record=initial))
    with gzip.open(HERE/'集中对照/全部调用.json.gz','wt',encoding='utf-8') as f:json.dump(calls,f,ensure_ascii=False,separators=(',',':'))
    with gzip.open(HERE/'集中对照/生成观察.json.gz','wt',encoding='utf-8') as f:json.dump(generations,f,ensure_ascii=False,separators=(',',':'))
    write_csv(HERE/'集中对照/逐配置比较.csv',pairs)
    atomic_json(HERE/'集中对照/晋级判定.json',dict(registered_rule='执行协议.md',scenes=results,
        logical_calls=len(calls),new_calls=sum(not c['record'].get('cache_hit',False) for c in calls),
        failures=sum(c['record']['status']!='success' for c in calls),execution=execution))
    atomic_json(HERE/'集中对照/改善记录.json',dict(records=winners))
    print(json.dumps(results,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
