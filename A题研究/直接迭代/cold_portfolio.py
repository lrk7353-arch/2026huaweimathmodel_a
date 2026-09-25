"""One-budget graph-to-plan composition of validated search neighborhoods.

Only the original graph and this run's charged observations are used. The
prefix and local search share exact-plan dedup, a call cap and a soft deadline.
`local_legacy` is the same framework with old local neighborhoods for ablation.
"""
import gzip
import math
from collections import defaultdict, deque

from common_run import DATA, R, GraphIR, atomic_json, read_json, validate_plan, evaluate, score
from pathlib import Path
import json
import time
from advanced_solver.component_baseline import generate_component_candidates
from advanced_solver.operation_assign import generate_operation_candidates
from controller import generate_wcc_candidates
from refine_regions import candidates as local_candidates


def exact(plan):
    return json.dumps(plan,ensure_ascii=False,separators=(',',':'))


def prefix_run(case,problem,cores,out,budget,seconds,timeout,evdir,proposal_budget=None):
    if problem==1:
        from p1_adaptive import run
        return run(case,cores,'portfolio_diverse',out,budget,seconds,seed=17,
                   evaluation_timeout=timeout,evaluation_dir=evdir)
    from p23_pipeline import run
    return run(case,problem,cores,'trace_routed',out,budget,seconds,seed=17,
               evaluation_timeout=timeout,evaluation_dir=evdir,proposal_budget=proposal_budget)


def local_order(ir,record,problem,variant):
    if variant in ('local_legacy','wide_legacy'):return ['legacy','legacy','structure','legacy'],{'reason':'legacy_ablation'}
    if problem==1:return ['joint','structure','joint','legacy'],{'reason':'P1_joint_with_structural_control'}
    from p23_data_refine import partition_copy_bytes
    from advanced_solver.trace_refine import _plan_assignment,_tensor_views
    plan=read_json(record['plan_path']);a=_plan_assignment(ir,plan,record['metrics']['num_cores']);views=_tensor_views(ir)
    replicated=sum(ir.tensors[t]['size']*(len({a[o] for o in readers})-1)
                   for t,readers in views[1].items() if readers and not views[0][t])
    ratio=replicated/max(1,partition_copy_bytes(ir,a,views))
    spill=record['metrics']['data_movement_bytes']['spill_added_copy_bytes']
    first=['data','region'] if spill>0 or ratio>=.35 else ['region','data']
    return first+['structure','legacy'],dict(reason='current_run_data_pressure',replicated_input_ratio=ratio,
        spill_bytes=spill,threshold=.35,scope='family ordering heuristic, not a proof of bottleneck')


def run(case,problem,cores,variant,out,budget=12,seconds=120,evaluation_timeout=25,evaluation_dir=None):
    if problem not in (1,2,3) or cores not in range(1,6) or variant not in ('integrated','local_legacy','wide_legacy','budget_greedy','budget_beam'):
        raise ValueError('invalid scene/cores/variant')
    budget_policy=variant in ('budget_greedy','budget_beam')
    if (budget_policy or variant=='wide_legacy') and problem==1:raise ValueError('budget variants currently require P2/P3')
    if type(budget) is not int or budget<1 or not math.isfinite(seconds) or seconds<=0:
        raise ValueError('positive finite call/time budget required')
    if not math.isfinite(evaluation_timeout) or evaluation_timeout<=0:raise ValueError('invalid timeout')
    out=Path(out).resolve()
    if out==DATA or DATA in out.parents:raise ValueError('output inside official data')
    out.mkdir(parents=True,exist_ok=False);started=time.monotonic();deadline=started+seconds
    evdir=Path(evaluation_dir) if evaluation_dir is not None else out/'evaluations'
    calls=[];stages=[];skips=[];seen=set();best=None
    prefix_cap=min(6,max(1,budget//2))
    atomic_json(out/'input.json',dict(case=case,problem=problem,cores=cores,variant=variant,budget=budget,
        seconds=seconds,prefix_cap=prefix_cap,evaluation_timeout=evaluation_timeout,seed=17))
    atomic_json(out/'progress.json',dict(case=case,problem=problem,num_cores=cores,method=variant,
        logical_calls=0,budget=budget,phase='prefix',prefix_cap=prefix_cap,complete=False))
    initial=prefix_run(case,problem,cores,out/'prefix',prefix_cap,max(.001,deadline-time.monotonic()),evaluation_timeout,evdir,
                       proposal_budget=budget if budget_policy or variant=='wide_legacy' else None)
    prefix_calls=initial.get('calls',initial.get('evaluations',[]))
    if len(prefix_calls)!=initial['logical_calls'] or len(prefix_calls)>prefix_cap:raise AssertionError('prefix overspent')
    for c in prefix_calls:
        calls.append(dict(c,stage='prefix'))
        if c['record'].get('plan_path'):seen.add(exact(read_json(c['record']['plan_path'])))
    best=initial.get('best_record')
    if best is not None and (best.get('status')!='success' or best['problem']!=problem or best['metrics']['num_cores']!=cores
                            or Path(best['graph_path']).stem!=case
                            or best['record_path'] not in {c['record'].get('record_path') for c in prefix_calls}):
        raise ValueError('prefix incumbent mismatch')
    stages.append(dict(phase='prefix',logical_calls=len(prefix_calls),elapsed_seconds=initial['elapsed_seconds']))
    if best is not None:atomic_json(out/'best.plan.json',read_json(best['plan_path']))
    ir=None
    def available():return len(calls)<budget and time.monotonic()<deadline
    def checkpoint(done=False):
        atomic_json(out/'progress.json',dict(case=case,problem=problem,num_cores=cores,method=variant,
            logical_calls=len(calls),budget=budget,best_record=best,complete=done,elapsed_seconds=time.monotonic()-started))
    checkpoint()
    if available():ir=GraphIR.from_path(DATA/(case+'.json'))
    structural=deque();paired=deque();component_best=initial.get('best_component')
    structure_ready=False;queues={};expanded=defaultdict(int);cursor=0
    order,decision=(local_order(ir,best,problem,variant) if best is not None and ir is not None
                    else (['structure'],{'reason':'no_prefix_success_or_no_budget'}))
    stages.append(dict(phase='family_order',order=order,decision=decision))
    allocation=None
    if budget_policy:
        from search_budget import SearchBudget
        allocation=SearchBudget(3 if variant=='budget_beam' else 1,order if best is not None else ['region','data','structure','legacy'])
        for call in prefix_calls:
            record=call['record']
            if record.get('status')=='success':
                if record['problem']!=problem or record['metrics']['num_cores']!=cores or Path(record['graph_path']).stem!=case:
                    raise ValueError('prefix beam record mismatch')
                allocation.add(record,best)
    if problem in (2,3) and component_best is not None and best is not None and available():
        # A large-window WCC candidate can be much better than the small-window
        # prefix. Expose one promising existing candidate before local moves
        # destroy that intact-component layout. The gate is heuristic only.
        t=time.monotonic()
        try:
            from p23_observed import select_wcc_probe
            proposals,_=generate_wcc_candidates(ir,read_json(component_best['plan_path']),num_cores=cores,
                max_candidates=max(12,budget),seed=17,policy='mixed')
            probe,details=select_wcc_probe(proposals,seen,exact,component_best['metrics'].get('capacity_bytes',{}))
            admitted=probe is not None and details['compute_proxy']<.9*score(best)[0]
            if admitted:paired.append(dict(probe,paired_source_record=component_best['record_path']))
            stages.append(dict(phase='prefix_wcc_lookahead',source_record=component_best['record_path'],selected=details,
                admitted=admitted,gate=.9,seconds=time.monotonic()-t,scope='compute-only proxy gate; not certified pruning'))
        except Exception as exc:
            stages.append(dict(phase='generation_error',family='prefix_wcc_lookahead',error=repr(exc),seconds=time.monotonic()-t))

    def generate_structure():
        nonlocal structure_ready
        if structure_ready:return
        structure_ready=True;t=time.monotonic();pools=[]
        cs,_=generate_component_candidates(ir,cores,max_candidates=max(12,budget),seed=17)
        pools.append([dict(c,structural_origin='component') for c in cs])
        if problem==1:
            from p1_selective import generate_selective_candidates
            from p1_portfolio import diversify
            cs,_=generate_selective_candidates(ir,cores,max_candidates=24,seed=17);pools.append(diversify(cs))
        else:
            # WCC neighborhoods require an intact-component parent. Retain the
            # prefix's component record even if its best assignment was split.
            comp=initial.get('best_component')
            if comp is not None:
                cs,_=generate_wcc_candidates(ir,read_json(comp['plan_path']),num_cores=cores,
                                            max_candidates=max(12,budget),seed=17,policy='mixed');pools.append(cs)
            cs,_=generate_operation_candidates(ir,cores,max_candidates=max(12,budget),seed=17);pools.append(cs)
        if problem==1:
            structural.extend(dict(c,source='original_graph') for i in range(max(map(len,pools),default=0)) for g in pools if i<len(g) for c in [g[i]])
        else:
            # Keep complete-layout alternatives reachable. Interleaving an
            # untried operation layout before every already-seen component
            # used to hide the next useful whole-component layout.
            structural.extend(c for group in pools for c in group)
        stages.append(dict(phase='structure_generation',candidate_count=len(structural),seconds=time.monotonic()-t))

    def next_candidate(family,selected_parent=None):
        if family in ('structure','structure_pair'):
            if family=='structure':generate_structure()
            pool=structural if family=='structure' else paired;parent=None
        else:
            if best is None:return None,None
            source=selected_parent if selected_parent is not None else best
            parent=source['record_path'];qkey=(parent,family)
            pool=queues.setdefault(qkey,deque())
            while not pool and expanded[qkey]<2 and available():
                index=expanded[qkey];expanded[qkey]+=1;t=time.monotonic()
                with gzip.open(source['result_path'],'rt') as f:raw=json.load(f)
                if raw['makespan']!=score(source)[0]:raise ValueError('trace/record score mismatch')
                cs,diag=local_candidates(ir,read_json(source['plan_path']),raw,problem,cores,family,index,24)
                pool.extend(cs);stages.append(dict(phase='local_generation',family=family,parent_record=parent,
                    round_index=index,count=len(cs),seconds=time.monotonic()-t,diagnostics=diag))
        while pool:
            c=pool.popleft();sig=exact(c['plan'])
            if sig in seen:continue
            seen.add(sig);validate_plan(ir,c['plan'])
            if len(c['plan']['core_schedules'])!=cores:raise ValueError('wrong candidate core count')
            bound=c.get('metadata',{}).get('lower_bound',0)
            if problem==1:
                from p1_selective import task_lower_bound
                from p1_boundary_lower_bound import boundary_ddr_lower_bound
                bound=max(bound,task_lower_bound(ir,c['plan'])['value'],boundary_ddr_lower_bound(ir,c['plan'])['lower_bound'])
                if best and bound>score(best)[0]:
                    skips.append(dict(name=c['name'],family=family,bound=bound,reason='fixed_candidate_necessary_bound'));continue
            c=dict(c,bound=bound)
            return c,c.get('paired_source_record',parent)
        if family not in ('structure','structure_pair') and expanded[parent,family]<2 and available():
            return next_candidate(family,selected_parent)
        return None,parent

    while available():
        evaluated=False
        for _ in range(16 if allocation else len(order)):
            selected_parent=None
            if paired:family='structure_pair'
            elif allocation:
                family,selected_parent=allocation.choose(best)
                if family is None:break
            else:family=order[cursor%len(order)];cursor+=1
            t=time.monotonic()
            try:c,parent=next_candidate(family,selected_parent)
            except Exception as exc:
                stages.append(dict(phase='generation_error',family=family,error=repr(exc),seconds=time.monotonic()-t))
                if allocation:allocation.exhaust(family,selected_parent)
                continue
            if c is None:
                if allocation:allocation.exhaust(family,selected_parent)
                continue
            if not available():continue
            candidate_started=t;before_span=score(best)[0] if best else None
            remaining=deadline-time.monotonic();trial=dict(name=c['name'],phase=family,parent_record=parent,
                metadata=c.get('metadata',{}),bound=c['bound'],accepted=False,stage='continuation')
            calls.append(trial)  # failures and wrapper exceptions consume a call
            try:r=evaluate(DATA/(case+'.json'),c['plan'],problem,evdir,
                timeout=min(evaluation_timeout,remaining),config_path=DATA/'config.txt')
            except Exception as exc:
                r=dict(status='wrapper_exception',error=repr(exc),cache_hit=False)
            trial['record']=r
            if r['status']=='success':
                if problem==1 and c['bound']>score(r)[0]:raise AssertionError('unsafe bound')
                if best is None or score(r)<score(best):
                    recovered=best is None
                    best=r;trial['accepted']=True;atomic_json(out/'best.plan.json',c['plan'])
                    if recovered:
                        order,decision=local_order(ir,best,problem,variant);cursor=0
                        stages.append(dict(phase='family_order',order=order,decision=decision,after_recovery=True))
                if allocation:allocation.add(r,best)
                if problem in (2,3) and c.get('structural_origin')=='component' and (component_best is None or score(r)<score(component_best)):
                    component_best=r
                    if available():
                        t=time.monotonic()
                        try:
                            from p23_observed import select_wcc_probe
                            proposals,_=generate_wcc_candidates(ir,c['plan'],num_cores=cores,max_candidates=max(12,budget),seed=17,policy='mixed')
                            probe,details=select_wcc_probe(proposals,seen,exact,r['metrics'].get('capacity_bytes',{}))
                            if probe is not None:paired.append(dict(probe,paired_source_record=r['record_path']))
                            stages.append(dict(phase='component_wcc_pair',source_record=r['record_path'],selected=details,seconds=time.monotonic()-t))
                        except Exception as exc:
                            stages.append(dict(phase='generation_error',family='structure_pair',error=repr(exc),seconds=time.monotonic()-t))
            if allocation:
                allocation.observe(family,selected_parent,before_span,score(best)[0] if best else before_span,time.monotonic()-candidate_started)
            trial['elapsed_seconds']=time.monotonic()-started;checkpoint();evaluated=True;break
        if not evaluated:break
    stop='time_budget' if time.monotonic()>=deadline else 'call_budget' if len(calls)>=budget else 'candidate_pools_exhausted'
    result=dict(case=case,problem=problem,num_cores=cores,method=variant,seed=17,budget=budget,soft_time_budget=seconds,
        effective_method=variant,
        prefix_cap=prefix_cap,logical_calls=len(calls),new_calls=sum(not c['record'].get('cache_hit',False) for c in calls),
        status='success' if best is not None else 'no_feasible_result',best_record=best,calls=calls,stages=stages,skipped=skips,
        elapsed_seconds=time.monotonic()-started,stop_reason=stop,evaluation_dir=str(evdir),
        scope='from original graph; no historical incumbent; prefix/failures/cache hits charged; all stages share a soft deadline')
    if allocation:result['budget_allocation']=allocation.summary()
    atomic_json(out/'summary.json',result);checkpoint(True);return result
