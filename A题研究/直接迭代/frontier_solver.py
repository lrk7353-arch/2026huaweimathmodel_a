"""One-budget cold solver: mature structural search then fast insertion repair.

The single-tail P23 variant reserves just one call, preserving all but the final
call of the mature trajectory. No historical library is read by this solver.
"""
import argparse
import json
from pathlib import Path
import time

from common_run import DATA, GraphIR, atomic_json, evaluate, read_json, score
from event_frontier import candidates


def mature(case,problem,cores,out,budget,proposal_budget,seconds,timeout):
    if problem==1:
        from cold_portfolio import run
        return run(case,problem,cores,'integrated',out,budget,seconds,timeout)
    from p23_pipeline import run
    return run(case,problem,cores,'trace_routed',out,budget,seconds,
               evaluation_timeout=timeout,evaluation_dir=Path(out)/'evaluations',
               proposal_budget=proposal_budget)


def run(case,problem,cores,out,budget=24,seconds=240,timeout=60,variant='frontier'):
    if variant not in ('mature','frontier','frontier_wide'):
        raise ValueError('unknown variant')
    if budget<2 or problem not in (1,2,3) or cores not in range(1,6):
        raise ValueError('invalid scenario/core count/budget')
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    started=time.monotonic();deadline=started+seconds
    reserve=0 if variant=='mature' else min(budget-1,4 if problem==1 or variant=='frontier_wide' else 1)
    atomic_json(out/'input.json',dict(case=case,problem=problem,cores=cores,variant=variant,
        budget=budget,seconds=seconds,timeout=timeout,reserve=reserve,
        scope='cold; shared total call cap and deadline; no historical input plans'))
    prefix=mature(case,problem,cores,out/'mature_prefix',budget-reserve,budget,
                  max(.001,deadline-time.monotonic()),timeout)
    calls=[dict(c,frontier_stage='mature_prefix') for c in prefix.get('calls',prefix.get('evaluations',[]))]
    assert len(calls)==prefix['logical_calls'] and len(calls)<=budget
    best=prefix.get('best_record');errors=[];generation_seconds=0.;proposals=[]
    seen={json.dumps(read_json(c['record']['plan_path']),ensure_ascii=False,separators=(',',':'))
          for c in calls if c['record'].get('plan_path')}
    def checkpoint(complete=False):
        result=dict(case=case,problem=problem,cores=cores,variant=variant,budget=budget,
            logical_calls=len(calls),calls=calls,best_record=best,reserve=reserve,
            prefix_calls=prefix['logical_calls'],generation_seconds=generation_seconds,
            generation_errors=errors,elapsed_seconds=time.monotonic()-started,complete=complete,
            scope='cold start; no historical plans; all successful, failed and cached calls charged')
        atomic_json(out/('summary.json' if complete else 'progress.json'),result)
        if best:atomic_json(out/'best.plan.json',read_json(best['plan_path']))
        return result
    checkpoint()
    if reserve and best and len(calls)<budget and time.monotonic()<deadline:
        ir=GraphIR.from_path(DATA/(case+'.json'));parent=read_json(best['plan_path'])
        stream=candidates(ir,problem,cores,parent,min(deadline,time.monotonic()+20))
        t=time.monotonic()
        try:
            if problem in (2,3) and variant=='frontier':
                proposals=[next(stream)]
            else:
                for proposal in stream:
                    if problem!=1 or proposal['metadata'].get('global_reassignment'):
                        proposals.append(proposal)
                # Compare like proxy models, and preserve each ordering family.
                groups={}
                for proposal in proposals:
                    groups.setdefault(proposal['metadata']['ordering'],[]).append(proposal)
                for group in groups.values():
                    group.sort(key=lambda c:c['metadata'].get('task_proxy',c['metadata']['proxy_finish']))
                order=['parent','critical','release','stable']
                proposals=[groups[name][i] for i in range(max(map(len,groups.values()),default=0))
                           for name in order if name in groups and i<len(groups[name])]
        except (StopIteration,TimeoutError,ValueError) as exc:
            errors.append(repr(exc))
        generation_seconds+=time.monotonic()-t
        # Previously yielded proposals remain usable if later generation times
        # out. The mature result remains available even if no proposal completes.
        for candidate in proposals:
            if len(calls)>=budget or time.monotonic()>=deadline:break
            signature=json.dumps(candidate['plan'],ensure_ascii=False,separators=(',',':'))
            if signature in seen:continue
            seen.add(signature)
            trial=dict(name=candidate['name'],metadata=candidate['metadata'],
                       frontier_stage='insertion_tail',accepted=False)
            calls.append(trial)
            try:
                record=evaluate(ir.path,candidate['plan'],problem,out/'evaluations',
                    timeout=min(timeout,max(.001,deadline-time.monotonic())),config_path=DATA/'config.txt')
            except Exception as exc:
                record=dict(status='wrapper_exception',error=repr(exc),cache_hit=False)
            trial['record']=record
            if record['status']=='success' and score(record)<score(best):
                best=record;trial['accepted']=True
            checkpoint()
    assert len(calls)<=budget
    return checkpoint(True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--case',type=int,required=True)
    p.add_argument('--problem',type=int,required=True);p.add_argument('--cores',type=int,default=5)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--budget',type=int,default=24)
    p.add_argument('--seconds',type=float,default=240);p.add_argument('--timeout',type=float,default=60)
    p.add_argument('--variant',choices=('mature','frontier','frontier_wide'),default='frontier')
    a=p.parse_args();s=run(f'case_{a.case:03d}',a.problem,a.cores,a.out,a.budget,a.seconds,a.timeout,a.variant)
    print(json.dumps(dict(calls=s['logical_calls'],best=score(s['best_record']) if s['best_record'] else None)))
