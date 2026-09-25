"""Scene-specific cold integration of hardware-inspired legal candidates.

P2/P3 use the measured single insertion tail. P1 reserves four calls for
coarse barrier-band partitions, selected by a Task-aware ranking surrogate.
All input plans are produced within this run and all calls share one cap.
"""
import argparse
import json
import math
from pathlib import Path
import time

from common_run import DATA, GraphIR, atomic_json, evaluate, read_json, score
from frontier_solver import mature, run as insertion_run
from barrier_bands import candidates


def run(case,problem,cores,out,budget=24,seconds=240,timeout=60,variant='frontier'):
    if type(budget) is not int or budget<2 or problem not in (1,2,3) or cores not in range(1,6):
        raise ValueError('invalid scenario, core count or total budget')
    if not all(math.isfinite(v) and v>0 for v in (seconds,timeout)):
        raise ValueError('positive finite time limits required')
    if variant not in ('mature','frontier'):raise ValueError('unknown variant')
    if problem!=1 or variant=='mature':
        return insertion_run(case,problem,cores,out,budget,seconds,timeout,variant)
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    start=time.monotonic();deadline=start+seconds;reserve=min(4,budget-1)
    prefix=mature(case,problem,cores,out/'mature_prefix',budget-reserve,budget,seconds,timeout)
    calls=list(prefix.get('calls',prefix.get('evaluations',[])))
    assert len(calls)==prefix['logical_calls'] and len(calls)<=budget
    best=prefix.get('best_record');generated=[];errors=[];generation_seconds=0.
    seen={c['record'].get('hashes',{}).get('plan_sha256') for c in calls}
    def save(complete=False):
        summary=dict(case=case,problem=problem,cores=cores,variant=variant,budget=budget,
            logical_calls=len(calls),calls=calls,best_record=best,reserve=reserve,
            prefix_calls=prefix['logical_calls'],generation_seconds=generation_seconds,
            generation_errors=errors,elapsed_seconds=time.monotonic()-start,complete=complete,
            scope='cold; P1 integrated prefix plus barrier bands; no historical plans')
        atomic_json(out/('summary.json' if complete else 'progress.json'),summary)
        if best:atomic_json(out/'best.plan.json',read_json(best['plan_path']))
        return summary
    save()
    if best and time.monotonic()<deadline and len(calls)<budget:
        ir=GraphIR.from_path(DATA/(case+'.json'));t=time.monotonic()
        try:
            for c in candidates(ir,1,cores,read_json(best['plan_path']),min(deadline,t+20)):
                generated.append(c)
        except (ValueError,TimeoutError) as exc:errors.append(repr(exc))
        generation_seconds=time.monotonic()-t
        generated.sort(key=lambda c:c['metadata']['task_proxy'])
        from solver.common import object_digest
        for c in generated:
            if time.monotonic()>=deadline or len(calls)>=budget:break
            signature=object_digest(c['plan'])
            if signature in seen:continue
            seen.add(signature);trial=dict(name=c['name'],metadata=c['metadata'],accepted=False,phase='barrier_tail')
            calls.append(trial)
            try:
                record=evaluate(ir.path,c['plan'],1,out/'evaluations',config_path=DATA/'config.txt',
                    timeout=min(timeout,max(.001,deadline-time.monotonic())))
            except Exception as exc:record=dict(status='wrapper_exception',error=repr(exc),cache_hit=False)
            trial['record']=record
            if record['status']=='success' and score(record)<score(best):best=record;trial['accepted']=True
            save()
    return save(True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--case',type=int,required=True)
    p.add_argument('--problem',type=int,required=True);p.add_argument('--cores',type=int,default=5)
    p.add_argument('--budget',type=int,default=24);p.add_argument('--seconds',type=float,default=240)
    p.add_argument('--timeout',type=float,default=60);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--variant',choices=('mature','frontier'),default='frontier');a=p.parse_args()
    s=run(f'case_{a.case:03d}',a.problem,a.cores,a.out,a.budget,a.seconds,a.timeout,a.variant)
    print(json.dumps(dict(calls=s['logical_calls'],best=score(s['best_record']) if s['best_record'] else None)))
