"""Supplement the final-step controls with whole-gain exact-plan P2/P3 pairs."""
import gzip,json,sys
from pathlib import Path
HERE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(HERE.parent))
from common_run import DATA,read_json,atomic_json,evaluate,write_csv,score

with gzip.open(HERE/'集中对照/全部调用.json.gz','rt') as f:calls=json.load(f)
results=[]
for case in ('case_023','case_046'):
    own=[c for c in calls if c['case']==case and c['problem']==3 and c['policy']=='wait_joint' and c['record']['status']=='success']
    before=next(c['record'] for c in own if c['call']==1);after=min((c['record'] for c in own),key=score)
    for label,record in [('initial',before),('final',after)]:
        for p in (2,3):
            replay=evaluate(DATA/(case+'.json'),read_json(record['plan_path']),p,
                HERE/'主要收益归因'/'evaluations'/case,timeout=60,config_path=DATA/'config.txt')
            assert replay['status']=='success'
            if p==3:assert score(replay)==score(record)
            results.append(dict(case=case,endpoint=label,problem=p,source_record=record,record=replay))
rows=[]
for r in results:
    m=r['record']['metrics'];d=m['data_movement_bytes'];c=m.get('cache_stats',{})
    rows.append(dict(case=r['case'],endpoint=r['endpoint'],problem=r['problem'],makespan=m['makespan'],
        copy=d['added_copy_bytes'],hit=c.get('hit_rate'),hit_bytes=c.get('hit_bytes'),miss_bytes=c.get('miss_bytes')))
atomic_json(HERE/'主要收益归因/记录.json',dict(logical_calls=len(results),records=results,
    scope='Exact initial and final plans, each under P2/P3. Whole-gain attribution; separate from previously recorded final-step controls.'))
write_csv(HERE/'主要收益归因/同方案缓存对照.csv',rows)
print(json.dumps(rows,ensure_ascii=False,indent=2))
