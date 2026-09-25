"""Experimental from-scratch portfolio with an explicit shared prefix/task budget.

Candidate families are reused from upstream 18e0957. Static and iterative arms
share all code and the same prefix. This module does not replace the default.
"""
import gzip
from collections import Counter
from common_run import *
from advanced_solver.component_baseline import generate_component_candidates
from advanced_solver.engine import generate_coarse_p1_candidates
from p1_selective import generate_selective_candidates, task_lower_bound
from p1_boundary_lower_bound import boundary_ddr_lower_bound
from p1_adaptive import plan_key
from p1_portfolio import diversify, refinement_caps


def run(case, cores, out, budget=12, seconds=180, seed=17, refresh=False, bottleneck=False):
    reservations = {8:3, 12:4, 16:6}
    if budget not in reservations or cores not in range(1,6) or seconds <= 0:
        raise ValueError('budgets 8/12/16, cores 1..5 and positive time required')
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    started=time.monotonic();deadline=started+seconds
    ir=GraphIR.from_path(DATA/(case+'.json'))
    calls=[];skipped=[];seen=set();best=None;generation=0
    prefix_cap=budget-reservations[budget]
    structure_cap=max(1,prefix_cap-2)
    events=[dict(stage='allocation',budget=budget,prefix_cap=prefix_cap,
                 structure_cap=structure_cap,task_reserve=reservations[budget])]

    def apply(pool,cap,phase,first_improvement=False):
        nonlocal best
        for c in pool:
            if len(calls)>=cap or time.monotonic()>=deadline:
                break
            signature=plan_key(c['plan'])
            if signature in seen:
                continue
            seen.add(signature)
            bound=c.get('metadata',{}).get('lower_bound')
            if bound is None:
                bound=max(task_lower_bound(ir,c['plan'])['value'],
                          boundary_ddr_lower_bound(ir,c['plan'])['lower_bound'])
            if best and bound>score(best['record'])[0]:
                skipped.append(dict(name=c['name'],phase=phase,bound=bound,reason='necessary_bound'))
                continue
            remaining=deadline-time.monotonic()
            if remaining<=0:
                break
            validate_plan(ir,c['plan'])
            parent=score(best['record']) if best else None
            rec=evaluate(DATA/(case+'.json'),c['plan'],1,out/'evaluations',
                         timeout=min(60,remaining),config_path=DATA/'config.txt')
            if rec['status']=='success' and bound>score(rec)[0]:
                raise AssertionError('unsafe lower bound')
            accepted=rec['status']=='success' and (best is None or score(rec)<score(best['record']))
            calls.append(dict(name=c['name'],phase=phase,generation=generation,
                              parent_score=parent,record=rec,accepted=accepted,bound=bound))
            if accepted:
                best=dict(name=c['name'],record=rec)
                atomic_json(out/'best.plan.json',c['plan'])
            atomic_json(out/'progress.json',dict(case=case,calls=len(calls),phase=phase,best=best))
            if accepted and first_improvement:
                return True
        return False

    component,_=generate_component_candidates(ir,cores,max_candidates=4,seed=seed)
    if len(ir.compute_ids)>6000:
        component=sorted(component,key=lambda c:max(Counter(c['plan']['node_to_subgraph'].values()).values(),default=0))
    apply(component,structure_cap,'component')
    extra=[]
    if len(calls)<structure_cap and time.monotonic()<deadline:
        extra,diag=generate_selective_candidates(ir,cores,max_candidates=24,seed=seed)
        if not diag['heavy_component_ids']:
            coarse,_=generate_coarse_p1_candidates(ir,cores,12,seed)
            eft=[c for c in coarse if c['metadata']['assignment']=='p1_eft']
            extra=eft[:2]+extra[:1]+eft[2:]+extra[1:]
        else:
            extra=diversify(extra)
        apply(extra,structure_cap,'partition')
    if best and len(calls)<prefix_cap and time.monotonic()<deadline:
        from p1_tensor_regions import generate
        tensors=sorted(generate(ir,cores),key=lambda c:(c['metadata']['lower_bound'],c['metadata']['proxy'],c['name']))
        gate=[c for c in tensors if c['metadata']['proxy']<=.85*score(best['record'])[0]]
        events.append(dict(stage='tensor_gate',generated=len(tensors),admitted=len(gate)))
        apply(gate,min(prefix_cap,len(calls)+2),'tensor')
    apply(component+extra,prefix_cap,'prefix_fallback')
    prefix_calls=len(calls)
    prefix_best=best
    if bottleneck and best and len(calls)<budget and time.monotonic()<deadline:
        # Same prefix as the old arm; reserve one old refinement, two distinct
        # repartition families, and one composed merge/diversity call.
        from p1_task_refine import generate as task_generate, view
        from p1_bottleneck_diagnose import diagnose, regions
        from p1_bottleneck_repartition import generate as regional_generate, merge_region
        plan=read_json(best['record']['plan_path'])
        with gzip.open(best['record']['result_path'],'rt') as f: raw=json.load(f)
        caps=refinement_caps(plan,cores)
        old=(task_generate(ir,plan,raw,seed,merge_caps=caps) if caps else [])+task_generate(ir,plan,raw,seed)
        apply(old,min(budget,len(calls)+1),'old_task_refine')
        plan=read_json(best['record']['plan_path'])
        with gzip.open(best['record']['result_path'],'rt') as f: raw=json.load(f)
        diagnostic=diagnose(ir,plan,raw)
        selected_regions=regions(ir,plan,diagnostic)
        construction_started=time.perf_counter()
        pool,diag=regional_generate(ir,plan,selected_regions,min(30,max(0,deadline-time.monotonic())))
        events.append(dict(stage='bottleneck_generation',diagnostic=diagnostic,regions=selected_regions,**diag))
        new_start=len(calls)
        chosen=[]
        for family in ('branch','affinity'):
            options=[c for c in pool if c['metadata']['family']==family and c['metadata']['width']==1]
            if options:
                greedy=min(options,key=lambda c:(c['metadata']['proxy'],c['name']))
                ident=greedy['name'].rsplit('_w',1)[0]+'_w4'
                candidate=next((c for c in pool if c['name']==ident),None)
                if candidate:
                    chosen.append(candidate)
                    apply([candidate],min(budget,len(calls)+1),'bottleneck_repartition')
        viable=[x for x in calls[new_start:] if x['record']['status']=='success']
        adaptive=[]
        if viable:
            parent=min(viable,key=lambda x:score(x['record']))
            candidate=next(c for c in chosen if c['name']==parent['name'])
            mp,_,_,_,_=view(ir,plan)
            protected={o for o,t in mp.items() if t not in candidate['metadata']['old_tasks']}
            # The parent need not beat the incumbent. Merge is evaluated only
            # as a complete candidate, allowing a losing split to be repaired.
            merged=merge_region(ir,candidate['plan'],protected,candidate['metadata']['target_work']*2,
                                min(time.perf_counter()+max(0,deadline-time.monotonic()),construction_started+30))
            adaptive.append(dict(name=candidate['name']+'_merge',plan=merged,metadata=dict(parent=parent['name'])))
        adaptive+=sorted((c for c in pool if c['metadata']['width']==4),key=lambda c:(c['metadata']['proxy'],c['name']))
        apply(adaptive,min(budget,len(calls)+1),'bottleneck_adaptive')
    while not bottleneck and best and len(calls)<budget and time.monotonic()<deadline:
        from p1_task_refine import generate
        plan=read_json(best['record']['plan_path'])
        caps=refinement_caps(plan,cores)
        if caps is None:
            break
        with gzip.open(best['record']['result_path'],'rt') as f:
            raw=json.load(f)
        pool=generate(ir,plan,raw,seed,merge_caps=caps) if caps else []
        pool+=generate(ir,plan,raw,seed)
        generation+=1
        improved=apply(pool,budget,'task_refine',first_improvement=refresh)
        if not refresh or not improved:
            break
    apply(component+extra,budget,'partition_fallback')
    result=dict(case=case,problem=1,num_cores=cores,
        variant='reserved_bottleneck' if bottleneck else ('reserved_iterative' if refresh else 'reserved_single'),budget=budget,
        prefix_cap=prefix_cap,prefix_calls=prefix_calls,prefix_best=prefix_best,
        task_call_reserve=reservations[budget],generations=generation,
        best=best,best_record=best['record'] if best else None,
        evaluations=calls,skipped=skipped,routing=events,logical_calls=len(calls),
        new_calls=sum(not x['record']['cache_hit'] for x in calls),
        elapsed_seconds=time.monotonic()-started,
        stop_reason='time_budget' if time.monotonic()>=deadline else 'budget_or_pool_exhausted',
        scope='from scratch; one shared call/time budget; explicit prefix/task reservation')
    atomic_json(out/'summary.json',result)
    return result
