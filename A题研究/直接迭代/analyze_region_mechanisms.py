"""Observed before/after explanations; correlation is not causal attribution."""
import argparse
import gzip
from common_run import *


def observation(record):
    with gzip.open(record['result_path'],'rt') as f:raw=json.load(f)
    tasks=[t for c in raw['per_core_timeline'] for t in c.get('tasks',[])]
    result=dict(makespan=score(record)[0],tasks=len(tasks),
                active_cores=sum(bool(c.get('tasks')) for c in raw['per_core_timeline']),
                added_copy_bytes=score(record)[1],
                spill_bytes=record['metrics']['data_movement_bytes'].get('spill_added_copy_bytes'))
    if record['problem']==1:
        result['longest_task_duration']=max((t['duration'] for t in tasks),default=0)
    else:
        ir=GraphIR.from_path(record['graph_path']);compute=set(ir.compute_ids);busy=[]
        for c in raw['per_core_timeline']:
            per_pipe={p:sum(o['duration'] for o in c.get('ops',[]) if o['op_id'] in compute and o['pipe']==p)
                      for p in ('PIPE_M','PIPE_V')}
            busy.append(max(per_pipe.values()))
        result['max_core_original_pipe_busy']=max(busy,default=0)
        result['cache_stats']=raw.get('cache_stats')
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--before',required=True);p.add_argument('--export',required=True,type=Path)
    a=p.parse_args();before={key(r):r for r in read_json(a.before)['records']};rows=[]
    for r in csv.DictReader((a.export/'改善清单.csv').open(encoding='utf-8-sig')):
        c,problem,n=r['case'],int(r['problem']),int(r['cores']);new=read_json(r['official_record'])
        old=before[c,problem,n]
        if score(new)[0]>=score(old)[0]:continue
        left,right=observation(old),observation(new)
        rows.append(dict(case=c,problem=problem,cores=n,reduction_pct=100*(1-right['makespan']/left['makespan']),
            before=left,after=right,source_record=new['record_path']))
    atomic_json(a.export/'结构变化分析.json',dict(rows=rows,
        scope='observed before/after features, not exclusive causal attribution; improved cases are selected'))
    print(json.dumps(dict(configurations=len(rows)),ensure_ascii=False))


if __name__=='__main__':main()
