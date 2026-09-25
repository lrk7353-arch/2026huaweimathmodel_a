"""Frozen-result four-cell attribution, without feeding outcomes back to search."""
import gzip
import json
from pathlib import Path
import sys
from concurrent.futures import ProcessPoolExecutor,as_completed
from collections import defaultdict
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parent))
from common_run import DATA,GraphIR,read_json,atomic_json,evaluate,write_csv,score,validate_plan
from advanced_solver.trace_refine import _plan_assignment,_runs_plan,_topology


def observation_order(ir,record):
    with gzip.open(record['result_path'],'rt') as f:raw=json.load(f)
    starts={o['op_id']:(o['start'],o['end'],o['op_id']) for c in raw['per_core_timeline'] for o in c['ops'] if o['op_id'] in ir.compute_ids}
    return _topology(ir,starts)


def p1_control(ir,old,new,which):
    base=old if which=='migration_only' else new
    assignment=_plan_assignment(ir,new if which=='migration_only' else old,5)
    members=defaultdict(list)
    for o,t in base['node_to_subgraph'].items():members[t].append(int(o))
    owners={}
    for t,ops in members.items():
        choices={assignment[o] for o in ops}
        if len(choices)!=1:return None,'Cannot express this single-factor control: Task spans multiple desired cores.'
        owners[t]=next(iter(choices))
    from p1_local_regions import task_order
    # Preserve a legal Task order under the original compute graph.
    order=task_order(ir,base);seq=[[] for _ in range(5)]
    for t in order:seq[owners[t]].append(t)
    value=dict(node_to_subgraph=base['node_to_subgraph'].copy(),core_schedules=seq)
    validate_plan(ir,value);return value,None


def worker(job):
    label,case,p,cell,plan=job
    rec=evaluate(DATA/(case+'.json'),plan,p,HERE/'归因'/label/'evaluations',timeout=60,config_path=DATA/'config.txt')
    return dict(label=label,case=case,problem=p,cell=cell,record=rec)


def main():
    with gzip.open(HERE/'集中对照/全部调用.json.gz','rt') as f:calls=json.load(f)
    pairs=list(__import__('csv').DictReader((HERE/'集中对照/逐配置比较.csv').open(encoding='utf-8-sig')))
    initial={(c['case'],c['problem'],c['policy']):c['record'] for c in calls if c['call']==1}
    selected=[]
    for p in (1,2,3):
        available=[c for c in calls if c['problem']==p and c['policy']=='wait_joint' and c['call']>1 and c['record']['status']=='success']
        best=min(available,key=lambda c:score(c['record'])[0]/score(initial[c['case'],p,'wait_joint'])[0])
        selected.append(('best',best))
        worst=max((r for r in pairs if int(r['problem'])==p),key=lambda r:float(r['wait_joint'])/float(r['mature']))
        choices=[c for c in available if c['case']==worst['case']]
        if choices:
            c=min(choices,key=lambda c:score(c['record']))
            if c['record']['record_path']!=best['record']['record_path']:selected.append(('counterexample',c))
    jobs=[];selection=[];skipped=[]
    for role,c in selected:
        case=c['case'];p=c['problem'];ir=GraphIR.from_path(DATA/(case+'.json'))
        parent=read_json(c['parent_record']);newrec=c['record'];old=read_json(parent['plan_path']);new=read_json(newrec['plan_path'])
        label=f'{case}_p{p}_{role}';controls={}
        if p==1:
            controls={'old':old,'joint':new}
            for which in ('migration_only','partition_only'):
                value,reason=p1_control(ir,old,new,which)
                if value is None:skipped.append(dict(label=label,cell=which,reason=reason))
                else:controls[which]=value
        else:
            a0=_plan_assignment(ir,old,5);a1=_plan_assignment(ir,new,5)
            o0=observation_order(ir,parent);o1=observation_order(ir,newrec)
            for ai,a in enumerate((a0,a1)):
                for oi,o in enumerate((o0,o1)):controls[f'a{ai}_o{oi}']=_runs_plan(ir,o,a,5,1)
            controls['old_exact']=old;controls['new_exact']=new
        for cell,plan in controls.items():
            validate_plan(ir,plan)
            for scene in ((2,3) if p==3 else (p,)):
                jobs.append((label,case,scene,cell,plan))
        selection.append(dict(label=label,role=role,case=case,problem=p,candidate_name=c['name'],
            metadata=c['metadata'],parent_record=parent,new_record=newrec,
            note='P23 four-cell plans use identical singleton encoding; exact endpoints are independent encoding controls.'))
    (HERE/'归因').mkdir(exist_ok=True);atomic_json(HERE/'归因/选择与控制.json',dict(selection=selection,skipped=skipped,jobs=len(jobs)))
    result=[]
    with ProcessPoolExecutor(max_workers=4) as pool:
        for future in as_completed([pool.submit(worker,j) for j in jobs]):
            item=future.result();result.append(item);print(item['label'],item['problem'],item['cell'],item['record']['status'],flush=True)
    atomic_json(HERE/'归因/官方记录.json',dict(logical_calls=len(result),records=result))
    rows=[]
    for r in result:
        m=r['record']['metrics'];d=m.get('data_movement_bytes',{});c=m.get('cache_stats',{})
        rows.append(dict(label=r['label'],problem=r['problem'],cell=r['cell'],status=r['record']['status'],
            makespan=m.get('makespan'),copy=d.get('added_copy_bytes'),spill=d.get('spill_added_copy_bytes'),
            hit=c.get('hit_rate'),hit_bytes=c.get('hit_bytes'),miss_bytes=c.get('miss_bytes')))
    write_csv(HERE/'归因/四格及缓存对照.csv',rows)


if __name__=='__main__':main()
