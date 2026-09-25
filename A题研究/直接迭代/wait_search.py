"""One charged warm-start budget, bounded generation and four diverse parents."""
from collections import deque
import gzip
import math
from pathlib import Path
import subprocess
import sys
import time

from common_run import DATA,GraphIR,read_json,atomic_json,evaluate,score,validate_plan
from critical_wait_candidates import signature


def archive(records,best,width=4):
    pool=[r for r in records if score(r)[0]<=1.05*score(best)[0]]
    def pressure(r):
        m=r['metrics'];peak=max((max(v.values(),default=0) for v in m['memory_peak_by_core'].values()),default=0)
        return m['data_movement_bytes']['spill_added_copy_bytes'],peak,score(r)
    selected=[best]
    for key in (lambda r:(score(r)[1],score(r)[0]),pressure):
        for r in sorted(pool,key=key):
            if r['hashes']['plan_sha256'] not in {x['hashes']['plan_sha256'] for x in selected}:
                selected.append(r);break
    from refine_regions import structure
    best_shape=structure(read_json(best['plan_path']))
    for r in sorted(pool,key=score):
        if r not in selected and structure(read_json(r['plan_path']))!=best_shape:
            selected.append(r);break
    return selected[:width]


def run(case,problem,cores,initial,out,policy,budget=12,seconds=180,timeout=45):
    if policy not in ('mature','proxy','wait_joint'):raise ValueError('invalid policy')
    if type(budget) is not int or budget<1 or not all(math.isfinite(x) and x>0 for x in (seconds,timeout)):raise ValueError('invalid budgets')
    if type(problem) is not int or problem not in (1,2,3):raise ValueError('problem must be 1, 2 or 3')
    if type(cores) is not int or cores not in range(1,6):raise ValueError('cores must be 1 through 5')
    if len(initial.get('core_schedules',[]))!=cores:raise ValueError('wrong target core count')
    started=time.monotonic();deadline=started+seconds
    ir=GraphIR.from_path(DATA/(case+'.json'));validate_plan(ir,initial)
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    calls=[];generations=[];best=None;records=[];expanded=set();seen=set();pending=deque();gen_seconds=0
    atomic_json(out/'input.json',dict(case=case,problem=problem,cores=cores,policy=policy,budget=budget,seconds=seconds,timeout=timeout))
    def checkpoint(done=False):
        summary=dict(case=case,problem=problem,cores=cores,policy=policy,budget=budget,logical_calls=len(calls),
            calls=calls,best_record=best,generations=generations,generation_seconds=gen_seconds,
            elapsed_seconds=time.monotonic()-started,complete=done,
            new_calls=sum(not c['record'].get('cache_hit',False) for c in calls),
            failures=sum(c['record']['status']!='success' for c in calls),
            generation_failures=sum(g['status']!='success' for g in generations))
        atomic_json(out/('summary.json' if done else 'progress.json'),summary);return summary
    def evaluate_one(candidate,parent=None):
        nonlocal best
        plan=candidate['plan'];validate_plan(ir,plan);seen.add(signature(plan))
        r=evaluate(DATA/(case+'.json'),plan,problem,out/'evaluations',
            timeout=min(timeout,max(.001,deadline-time.monotonic())),config_path=DATA/'config.txt')
        accepted=r['status']=='success' and (best is None or score(r)<score(best))
        if r['status']=='success':
            records.append(r)
            if accepted:best=r;atomic_json(out/'best.plan.json',plan)
        calls.append(dict(name=candidate['name'],metadata=candidate.get('metadata',{}),parent_record=parent,
            record=r,accepted=accepted,elapsed_seconds=time.monotonic()-started,best_makespan=score(best)[0] if best else None))
        checkpoint()
    evaluate_one(dict(name='charged_initial',plan=initial))
    since_generation=4
    while best and len(calls)<budget and time.monotonic()<deadline:
        eligible=[r for r in archive(records,best) if r['hashes']['plan_sha256'] not in expanded]
        if eligible and (not pending or since_generation>=4):
            parent=eligible[0];expanded.add(parent['hashes']['plan_sha256']);idx=len(generations)
            request=dict(case=case,problem=problem,cores=cores,policy=policy,round=0,limit=32,
                plan=parent['plan_path'],result=parent['result_path'])
            atomic_json(out/f'generation_{idx}.input.json',request)
            t=time.monotonic();limit=min(20,max(.001,deadline-t));destination=out/f'generation_{idx}.json'
            try:
                completed=subprocess.run([sys.executable,str(Path(__file__).with_name('wait_candidate_worker.py')),
                    str(out/f'generation_{idx}.input.json'),str(destination)],capture_output=True,text=True,timeout=limit)
                generated=read_json(destination) if completed.returncode==0 and destination.exists() else dict(
                    status='generation_error',error=completed.stderr[-2000:],candidates=[])
            except subprocess.TimeoutExpired:
                generated=dict(status='generation_timeout',candidates=[])
            duration=time.monotonic()-t;gen_seconds+=duration
            generations.append(dict(index=idx,parent=parent['record_path'],status=generated['status'],elapsed_seconds=duration,
                candidate_count=len(generated['candidates']),error=generated.get('error'),diagnostics=generated.get('diagnostics')))
            # Preserve all old-parent candidates; new parent earns a finite head lease.
            fresh=[dict(c,parent_record=parent['record_path']) for c in generated['candidates']]
            pending.extendleft(reversed(fresh[:4]));pending.extend(fresh[4:]);since_generation=0
            checkpoint()
        if not pending:break
        c=pending.popleft()
        if signature(c['plan']) in seen:continue
        if time.monotonic()>=deadline:break
        evaluate_one(c,c['parent_record']);since_generation+=1
    return checkpoint(True)


def main():
    import argparse
    import json
    parser=argparse.ArgumentParser(description='Charged warm refinement of an explicit plan; no historical cache required.')
    parser.add_argument('--case',required=True,help='Graph name, for example case_023')
    parser.add_argument('--problem',type=int,choices=(1,2,3),required=True)
    parser.add_argument('--cores',type=int,choices=range(1,6),required=True)
    parser.add_argument('--incumbent-plan',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True,help='New output directory; must not already exist')
    parser.add_argument('--policy',choices=('mature','proxy','wait_joint'),default='wait_joint')
    parser.add_argument('--budget',type=int,default=12,help='Total official calls, including initial plan and failures')
    parser.add_argument('--seconds',type=float,default=180)
    parser.add_argument('--timeout',type=float,default=45)
    args=parser.parse_args()
    result=run(args.case,args.problem,args.cores,read_json(args.incumbent_plan),args.out,args.policy,
               args.budget,args.seconds,args.timeout)
    best=result['best_record']
    print(json.dumps(dict(output=str(args.out.resolve()),logical_calls=result['logical_calls'],failures=result['failures'],
                          generation_failures=result['generation_failures'],best_score=score(best) if best else None,
                          elapsed_seconds=result['elapsed_seconds']),ensure_ascii=False))


if __name__=='__main__':main()
