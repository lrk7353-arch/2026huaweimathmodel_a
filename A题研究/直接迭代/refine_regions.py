"""Bounded iterative regional refinement for all three official scenarios.

Each accepted improvement gets a newly observed neighborhood. A small beam can
also expand near-best, structurally different plans. Legacy and regional arms
use this same loop for controlled warm-start comparisons.
"""
import argparse
import gzip
import math
from collections import defaultdict, deque

from common_run import *


def exact(plan):
    return json.dumps(plan, ensure_ascii=False, separators=(',', ':'))


def structure(plan):
    # Only beam diversity uses a label-independent partition; evaluator dedup
    # always uses exact ordered serialization, as priorities can depend on it.
    members=defaultdict(list)
    for o,s in plan['node_to_subgraph'].items():members[s].append(int(o))
    return tuple(tuple(tuple(sorted(members[s])) for s in seq) for seq in plan['core_schedules'])


def candidates(ir, plan, raw, problem, cores, policy, round_index, limit):
    if policy == 'data':
        if problem not in (2,3): raise ValueError('data policy requires P2/P3')
        from p23_data_refine import generate
        return generate(ir,plan,raw,cores,limit,round_index)
    if problem == 1:
        if policy == 'legacy':
            from p1_task_refine import generate
            return generate(ir,plan,raw,seed=17+round_index),dict(family='old_task_templates')
        from p1_joint_regions import generate
        return generate(ir,plan,raw,cores,limit,round_index)
    if policy == 'legacy':
        from advanced_solver.trace_refine import generate_trace_candidates
        first,diag=generate_trace_candidates(ir,plan,raw,num_cores=cores,max_candidates=limit,round_index=round_index,seed=17)
        if problem == 3:
            from advanced_solver.cache_refine import generate_cache_candidates
            from p3_read_order import generate
            second,_=generate_cache_candidates(ir,plan,raw,num_cores=cores,max_candidates=limit,round_index=round_index,seed=17)
            third,_=generate(ir,plan,raw,cores,limit)
            first=[g[i] for i in range(max(len(first),len(second),len(third))) for g in (first,second,third) if i<len(g)]
        return first,diag
    if policy == 'reads' and problem != 3: raise ValueError('reads policy requires P3')
    pools=[];diagnostics={}
    if policy != 'reads':
        from p23_region_refine import generate
        cs,diag=generate(ir,plan,raw,cores,limit,round_index)
        pools.append(cs);diagnostics['region']=diag
    if problem == 3 and policy != 'region':
        from p3_joint_reads import generate
        cs,diag=generate(ir,plan,raw,cores,limit,round_index)
        pools.append(cs);diagnostics['reads']=diag
    return [g[i] for i in range(max(map(len,pools),default=0)) for g in pools if i<len(g)],diagnostics


def run(case, old, out, budget=24, seconds=180, cores=5, policy='joint',
        evaluation_dir=None, evaluation_timeout=45, beam_width=3):
    problem=old['problem']
    if old.get('status')!='success' or key(old)!=(case,problem,cores): raise ValueError('invalid incumbent')
    if (policy not in ('legacy','joint','region','reads','data') or
        (policy=='reads' and problem!=3) or (policy=='data' and problem==1)):raise ValueError('invalid policy')
    if type(budget) is not int or budget<1 or not math.isfinite(seconds) or seconds<=0:raise ValueError('positive budget required')
    if not math.isfinite(evaluation_timeout) or evaluation_timeout<=0 or beam_width<1:raise ValueError('invalid timeout/beam')
    out=Path(out).resolve()
    if out==DATA or DATA in out.parents:raise ValueError('output inside official data')
    out.mkdir(parents=True,exist_ok=False)
    started=time.monotonic();deadline=started+seconds
    ir=GraphIR.from_path(DATA/(case+'.json'));initial=read_json(old['plan_path'])
    validate_plan(ir,initial)
    if len(initial['core_schedules'])!=cores:raise ValueError('incumbent cores mismatch')
    evaldir=Path(evaluation_dir) if evaluation_dir is not None else R/'advanced_solver/runs/formal_v2/evaluations'
    best=old;calls=[];generation=[];skips=[];seen={exact(initial)}
    beam=[old];expanded=defaultdict(int);rounds=0;stagnant=0;generation_seconds=0.
    pending=deque();parent=None
    atomic_json(out/'input.json',dict(record=old,policy=policy,budget=budget,seconds=seconds,beam_width=beam_width))
    atomic_json(out/'best.plan.json',initial)

    def checkpoint(done=False):
        state=dict(case=case,problem=problem,num_cores=cores,policy=policy,before=score(old)[0],after=score(best)[0],
                   budget=budget,logical_calls=len(calls),best_record=best,elapsed_seconds=time.monotonic()-started,
                   generations=len(generation),complete=done)
        atomic_json(out/'progress.json',state)

    def update_beam(record):
        if score(record)[0] > 1.03*score(best)[0]:return
        pool=sorted(beam+[record],key=score);kept=[];shapes=set()
        for r in pool:
            shape=structure(read_json(r['plan_path']))
            if shape not in shapes: kept.append(r);shapes.add(shape)
            if len(kept)>=beam_width:break
        beam[:]=kept

    checkpoint()
    while len(calls)<budget and time.monotonic()<deadline:
        eligible=[r for r in beam if expanded[r['record_path']]<2]
        # Best parents first; a near-best alternative receives an expansion when
        # the best has used its two differently scaled neighborhoods.
        if eligible:
            parent=min(eligible,key=lambda r:(r is not best,expanded[r['record_path']],score(r)))
            index=expanded[parent['record_path']];expanded[parent['record_path']]+=1;rounds+=1
            plan=read_json(parent['plan_path'])
            with gzip.open(parent['result_path'],'rt') as f:raw=json.load(f)
            if raw['makespan']!=score(parent)[0]:raise ValueError('record/result makespan mismatch')
            t=time.monotonic()
            cs,diag=candidates(ir,plan,raw,problem,cores,policy,index,limit=24)
            generation_seconds+=time.monotonic()-t
            gen=dict(round=rounds,parent_record=parent['record_path'],parent_makespan=score(parent)[0],
                     parent_round=index,count=len(cs),elapsed_seconds=time.monotonic()-t,diagnostics=diag,
                     candidates=[dict(name=c['name'],metadata=c['metadata']) for c in cs])
            generation.append({k:v for k,v in gen.items() if k not in ('diagnostics','candidates')})
            atomic_json(out/f'generation_{rounds:02d}.json',gen)
            new=[dict(c,parent_record=parent['record_path'],generation=rounds) for c in cs]
            # Retain untried candidates from old parents while prioritizing fresh
            # improvements. Nothing is deemed bad simply for missing a proxy gate.
            pending.extendleft(reversed(new))
        elif not pending:break
        evaluated=0;improved=False
        while pending and len(calls)<budget and time.monotonic()<deadline:
            c=pending.popleft();sig=exact(c['plan'])
            if sig in seen:continue
            seen.add(sig)
            bound=c['metadata'].get('lower_bound',0)
            if problem==1 and bound>score(best)[0]:
                skips.append(dict(name=c['name'],reason='fixed_candidate_necessary_bound',bound=bound));continue
            validate_plan(ir,c['plan'])
            remaining=deadline-time.monotonic()
            if remaining<=0:break
            r=evaluate(DATA/(case+'.json'),c['plan'],problem,evaldir,
                       timeout=min(evaluation_timeout,remaining),config_path=DATA/'config.txt')
            if r['status']=='success' and problem==1 and bound>score(r)[0]:raise AssertionError('unsafe P1 bound')
            accepted=r['status']=='success' and score(r)<score(best)
            if accepted:
                best=r;improved=True;atomic_json(out/'best.plan.json',c['plan'])
            if r['status']=='success':update_beam(r)
            calls.append(dict(name=c['name'],metadata=c['metadata'],parent_record=c['parent_record'],
                generation=c['generation'],record=r,accepted=accepted,elapsed_seconds=time.monotonic()-started,
                best_makespan=score(best)[0]))
            evaluated+=1;checkpoint()
            if accepted:break  # reobserve the newly accepted schedule immediately
            if evaluated>=8 and eligible:break
        stagnant=0 if improved else stagnant+1
        if not pending and stagnant>=2:break
    stop='time_budget' if time.monotonic()>=deadline else 'call_budget' if len(calls)>=budget else 'neighborhood_exhausted'
    result=dict(case=case,problem=problem,num_cores=cores,policy=policy,before=score(old)[0],after=score(best)[0],
        best_record=best,calls=calls,budget=budget,logical_calls=len(calls),new_calls=sum(not c['record']['cache_hit'] for c in calls),
        failed_calls=sum(c['record']['status']!='success' for c in calls),generations=generation,skipped=skips,
        generation_seconds=generation_seconds,elapsed_seconds=time.monotonic()-started,stop_reason=stop,
        scope='warm-start, additional budget; beam scores are official; rankings are heuristics; not a from-scratch benchmark')
    atomic_json(out/'summary.json',result);checkpoint(True)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--case',type=int,required=True,choices=range(1,101));p.add_argument('--problem',type=int,required=True,choices=(1,2,3))
    p.add_argument('--cores',type=int,default=5,choices=range(1,6));p.add_argument('--out',type=Path,required=True)
    p.add_argument('--incumbent-plan',type=Path);p.add_argument('--policy',choices=('joint','legacy','region','reads','data'),
        help='default: joint for P1/P2, legacy for P3; joint/reads remain explicit P3 experiments')
    p.add_argument('--budget',type=int,default=24);p.add_argument('--seconds',type=float,default=180)
    p.add_argument('--evaluation-timeout',type=float,default=45);p.add_argument('--fresh-evaluations',action='store_true')
    a=p.parse_args();case=f'case_{a.case:03d}';started=time.monotonic();budget=a.budget;seconds=a.seconds
    a.policy=a.policy or ('legacy' if a.problem==3 else 'joint')
    if a.out.exists():p.error('use a new output directory')
    if a.out.resolve()==DATA or DATA in a.out.resolve().parents:p.error('outputs must be outside official data')
    if budget<1 or not math.isfinite(seconds) or seconds<=0:p.error('positive finite budget/time required')
    if not math.isfinite(a.evaluation_timeout) or a.evaluation_timeout<=0:p.error('positive finite evaluation-timeout required')
    if a.policy=='reads' and a.problem!=3:p.error('reads policy requires P3')
    if a.policy=='data' and a.problem==1:p.error('data policy requires P2/P3')
    out=a.out
    if a.incumbent_plan:
        if budget<2:p.error('portable search needs initial evaluation plus at least one refinement call')
        old=run_candidate(case,a.problem,a.cores,read_json(a.incumbent_plan),out/'initial_evaluation',timeout=min(seconds,a.evaluation_timeout))
        if old['status']!='success':raise RuntimeError('initial evaluation failed: '+old['status'])
        budget-=1;seconds-=time.monotonic()-started;out=out/'search'
        if seconds<=0:raise RuntimeError('initial evaluation consumed total time budget')
    else:old=known()[case,a.problem,a.cores]
    s=run(case,old,out,budget,seconds,a.cores,a.policy,
          evaluation_dir=a.out/'evaluations' if a.fresh_evaluations else None,evaluation_timeout=a.evaluation_timeout)
    if a.incumbent_plan:
        s={**s,'initial_record':old,'logical_calls':s['logical_calls']+1,'new_calls':s['new_calls']+int(not old['cache_hit']),
           'elapsed_seconds':time.monotonic()-started}
        atomic_json(a.out/'summary.json',s);atomic_json(a.out/'best.plan.json',read_json(s['best_record']['plan_path']))
    print(json.dumps({k:s[k] for k in ('case','problem','before','after','logical_calls','new_calls','elapsed_seconds','stop_reason')}))


if __name__=='__main__':main()
