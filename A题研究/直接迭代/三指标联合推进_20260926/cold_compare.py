"""Frozen conditional B24 comparison; all seeds are paid and generated cold."""
from collections import deque
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor,as_completed
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parent))
from common_run import DATA,R,read_json,atomic_json,evaluate,score,write_csv
from p23_pipeline import run as strong
from critical_wait_candidates import signature


def integrated(case,p,n,out):
    out.mkdir(parents=True,exist_ok=False);started=time.monotonic();deadline=started+180
    prefix=strong(case,p,n,'trace_routed',out/'prefix',7,180,evaluation_timeout=45,
        evaluation_dir=out/'evaluations',proposal_budget=24)
    calls=prefix['calls'];best=prefix['best_record'];component=prefix.get('best_component')
    seen={signature(read_json(c['record']['plan_path'])) for c in calls if c['record'].get('plan_path')}
    pending=deque();generated=set();generations=[];since_generation=6
    while best and len(calls)<24 and time.monotonic()<deadline:
        parent=best['hashes']['plan_sha256']
        if parent not in generated and (not pending or since_generation>=6):
            generated.add(parent);i=len(generations)
            request=dict(case=case,problem=p,cores=n,policy='mature',round=0,limit=24,plan=best['plan_path'],
                result=best['result_path'],component_plan=component['plan_path'] if component else None)
            atomic_json(out/f'gen{i}.input.json',request);dest=out/f'gen{i}.json';t=time.monotonic()
            try:
                proc=subprocess.run([sys.executable,str(HERE/'cold_worker.py'),str(out/f'gen{i}.input.json'),str(dest)],
                    capture_output=True,text=True,timeout=min(20,max(.001,deadline-t)))
                result=read_json(dest) if proc.returncode==0 and dest.exists() else dict(status='generation_error',candidates=[],error=proc.stderr[-2000:])
            except subprocess.TimeoutExpired:result=dict(status='generation_timeout',candidates=[])
            generations.append(dict(parent=parent,status=result['status'],seconds=time.monotonic()-t,
                candidate_count=len(result['candidates']),diagnostics=result.get('diagnostics')))
            # New parents do not erase untried original-parent candidates.
            fresh=[dict(c,parent_record=best['record_path']) for c in result['candidates']]
            pending.extendleft(reversed(fresh[:3]));pending.extend(fresh[3:]);since_generation=0
        if not pending:break
        c=pending.popleft();sig=signature(c['plan'])
        if sig in seen:continue
        seen.add(sig)
        if time.monotonic()>=deadline:break
        r=evaluate(DATA/(case+'.json'),c['plan'],p,out/'evaluations',
            timeout=min(45,max(.001,deadline-time.monotonic())),config_path=DATA/'config.txt')
        accepted=r['status']=='success' and score(r)<score(best)
        if accepted:best=r
        calls.append(dict(name=c['name'],phase=c['cold_family'],metadata=c.get('metadata',{}),record=r,accepted=accepted,
            parent_record=c['parent_record'],elapsed_seconds=time.monotonic()-started))
        since_generation+=1
        atomic_json(out/'progress.json',dict(calls=len(calls),best=best,elapsed_seconds=time.monotonic()-started))
    assert len(calls)<=24
    result=dict(case=case,problem=p,cores=n,variant='wait_integrated',calls=calls,best_record=best,
        logical_calls=len(calls),generations=generations,prefix_summary=str(out/'prefix/summary.json'),
        elapsed_seconds=time.monotonic()-started,scope='Cold B24. Paid 7-call structural prefix generated with proposal_budget24; joint/mature/intact-WCC queues share the remainder.')
    atomic_json(out/'summary.json',result)
    if best:atomic_json(out/'best.plan.json',read_json(best['plan_path']))
    return result


def worker(job):
    p,i,n,variant,group=job;case=f'case_{i:03d}';out=HERE/'从头对照'/'slots'/f'{case}_p{p}_n{n}'/variant
    if (out/'summary.json').exists():s=read_json(out/'summary.json')
    elif variant=='strong':s=strong(case,p,n,'trace_routed',out,24,180,evaluation_timeout=45,evaluation_dir=out/'evaluations')
    else:s=integrated(case,p,n,out)
    calls=s['calls'];best=s['best_record'];m=best['metrics'] if best else {};d=m.get('data_movement_bytes',{})
    row=dict(case=case,problem=p,cores=n,variant=variant,group=group,makespan=m.get('makespan'),
        added_copy=d.get('added_copy_bytes'),logical_calls=len(calls),elapsed_seconds=s['elapsed_seconds'],
        failures=sum(c['record']['status']!='success' for c in calls),generation_failures=sum(g['status']!='success' for g in s.get('generations',[])),
        summary=str(out/'summary.json'))
    for b in (8,16,24):
        valid=[c['record'] for c in calls[:b] if c['record']['status']=='success']
        row[f'B{b}']=score(min(valid,key=score))[0] if valid else None
    return row


def main():
    scenes=[s['problem'] for s in read_json(HERE/'集中对照/晋级判定.json')['scenes'] if s['passed']]
    assert all(p in (2,3) for p in scenes),'P1 requires a separately frozen cold implementation'
    assert scenes,'No scenario passed warm gate'
    dev={2:[53,62,80,18,9,90],3:[17,23,80,44,46,90]};jobs=[]
    for p in scenes:
        for group,ids,cores in [('development',dev[p],[5]),('decision',[13,28,37,55,71,92],[5]),('lowcore',[28,71],[2,3])]:
            jobs.extend((p,i,n,v,group) for i in ids for n in cores for v in ('strong','wait_integrated'))
    random.Random(17).shuffle(jobs);out=HERE/'从头对照';out.mkdir(exist_ok=True)
    sources={str(f.relative_to(R.parent)):hashlib.sha256(f.read_bytes()).hexdigest()
        for folder in (HERE,HERE.parent,R/'solver',R/'advanced_solver',R/'精修求解器') for f in folder.glob('*.py')}
    manifest=dict(scenes=scenes,jobs=jobs,sources=sources,git_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        budget=24,seconds=180,timeout=45,generation_timeout=20,seed=17,workers=4)
    if (out/'manifest.json').exists():assert read_json(out/'manifest.json')['sources']==sources
    else:atomic_json(out/'manifest.json',manifest)
    rows=[];errors=[];started=time.monotonic()
    with ProcessPoolExecutor(max_workers=4) as pool:
        for future in as_completed([pool.submit(worker,j) for j in jobs]):
            try:row=future.result();rows.append(row);print(row,flush=True)
            except Exception as e:errors.append(repr(e));print(repr(e),flush=True)
            write_csv(out/'逐臂结果.csv',rows);atomic_json(out/'progress.json',dict(done=len(rows),expected=len(jobs),errors=errors,seconds=time.monotonic()-started))
    atomic_json(out/'execution.json',dict(complete=len(rows)==len(jobs),errors=errors,calls=sum(r['logical_calls'] for r in rows),seconds=time.monotonic()-started))
    assert not errors,errors


if __name__=='__main__':main()
