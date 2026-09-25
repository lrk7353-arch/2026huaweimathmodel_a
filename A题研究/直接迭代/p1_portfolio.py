"""One-budget P1 search with graph-derived routing, without historical incumbents."""
import gzip,statistics,time
from collections import Counter,defaultdict
from common_run import *
from advanced_solver.component_baseline import generate_component_candidates
from advanced_solver.engine import generate_coarse_p1_candidates
from p1_selective import generate_selective_candidates,task_lower_bound
from p1_boundary_lower_bound import boundary_ddr_lower_bound
from p1_adaptive import plan_key


def refinement_caps(plan,cores):
    sizes=Counter(plan['node_to_subgraph'].values())
    # A single Task has no movable Task-level parallelism. Preserve call budget
    # for structural splitting; this is a routing heuristic, not a safe prune.
    if len(sizes)<=1:return None
    return [8,16] if len(sizes)>=4*cores and statistics.median(sizes.values())<=8 else []


def diversify(candidates):
    """Expose partition scales before spending calls on more variants of one scale."""
    groups=defaultdict(list)
    for c in candidates:
        meta=c['metadata'];groups[meta['partition'],meta['scale']].append(c)
    return [g[i] for i in range(max(map(len,groups.values()),default=0)) for g in groups.values() if i<len(g)]


def run(case,cores,out,budget=12,time_budget=180,seed=17,evaluation_timeout=None,structural_diversity=False,evaluation_dir=None):
    if budget<1 or time_budget<=0 or cores not in range(1,6):raise ValueError('invalid budget/time/cores')
    if evaluation_timeout is not None and evaluation_timeout<=0:raise ValueError('positive evaluation timeout required')
    out=Path(out);out.mkdir(parents=True,exist_ok=False);start=time.monotonic();deadline=start+time_budget
    evaluation_dir=Path(evaluation_dir) if evaluation_dir is not None else R/'advanced_solver/runs/formal_v2/evaluations'
    ir=GraphIR.from_path(DATA/(case+'.json'));calls=[];skipped=[];seen=set();best=None;events=[]
    timeout=evaluation_timeout if evaluation_timeout is not None else (180 if len(ir.compute_ids)>10000 else 60)
    component,_=generate_component_candidates(ir,cores,max_candidates=4,seed=seed)
    if structural_diversity and len(ir.compute_ids)>6000:
        # Start with smaller Tasks to obtain an incumbent before expensive
        # monolithic compilation consumes the time window. This only reorders.
        component=sorted(component,key=lambda c:max(Counter(c['plan']['node_to_subgraph'].values()).values(),default=0))
        events.append(dict(stage='component_order',rule='smallest_max_task_first',reason='large_graph_feasibility_first'))
    def apply(pool,cap,phase):
        nonlocal best
        for c in pool:
            if len(calls)>=cap or time.monotonic()>=deadline:break
            signature=plan_key(c['plan'])
            if signature in seen:continue
            seen.add(signature)
            meta=c.get('metadata',{})
            bound=meta.get('lower_bound')
            if bound is None:bound=max(task_lower_bound(ir,c['plan'])['value'],boundary_ddr_lower_bound(ir,c['plan'])['lower_bound'])
            if best and bound>score(best['record'])[0]:
                skipped.append(dict(name=c['name'],phase=phase,reason='necessary_bound',bound=bound));continue
            remaining=deadline-time.monotonic()
            if remaining<=0:break
            validate_plan(ir,c['plan'])
            rec=evaluate(DATA/(case+'.json'),c['plan'],1,evaluation_dir,
                         timeout=min(timeout,remaining),config_path=DATA/'config.txt')
            if rec['status']=='success' and bound>score(rec)[0]:raise AssertionError('unsafe lower bound')
            accepted=rec['status']=='success' and (best is None or score(rec)<score(best['record']))
            calls.append(dict(name=c['name'],phase=phase,record=rec,accepted=accepted,bound=bound))
            if accepted:
                best=dict(name=c['name'],record=rec);atomic_json(out/'best.plan.json',c['plan'])
            atomic_json(out/'progress.json',dict(case=case,calls=len(calls),phase=phase,best=best))
    reserve=min(4,budget//3);prefix_cap=budget-reserve
    apply(component,prefix_cap,'component');extra=[]
    if len(calls)<prefix_cap and time.monotonic()<deadline:
        extra,diag=generate_selective_candidates(ir,cores,max_candidates=24,seed=seed)
        if not diag['heavy_component_ids']:
            coarse,_=generate_coarse_p1_candidates(ir,cores,12,seed)
            eft=[c for c in coarse if c['metadata']['assignment']=='p1_eft']
            extra=eft[:2]+extra[:1]+eft[2:]+extra[1:]
        elif structural_diversity:
            extra=diversify(extra)
        apply(extra,prefix_cap,'partition')
    prefix_calls=len(calls)
    if best and len(calls)<budget and time.monotonic()<deadline:
        from p1_tensor_regions import generate as tensor_candidates
        # Proxy gating is only a heuristic budget choice. Necessary bounds are
        # checked independently in apply and are the only certified pruning.
        tensors=sorted(tensor_candidates(ir,cores),key=lambda c:(c['metadata']['lower_bound'],c['metadata']['proxy'],c['name']))
        gate=[c for c in tensors if c['metadata']['proxy']<=.85*score(best['record'])[0]]
        events.append(dict(stage='tensor_gate',generated=len(tensors),admitted=len(gate),ratio=.85))
        apply(gate,min(budget,len(calls)+2),'tensor')
    if best and len(calls)<budget and time.monotonic()<deadline:
        from p1_task_refine import generate as task_candidates
        plan=read_json(best['record']['plan_path']);caps=refinement_caps(plan,cores)
        events.append(dict(stage='task_route',tasks=len(set(plan['node_to_subgraph'].values())),small_merge_caps=caps))
        if caps is not None:
            with gzip.open(best['record']['result_path'],'rt') as f:raw=json.load(f)
            local=task_candidates(ir,plan,raw,seed,merge_caps=caps) if caps else []
            local+=task_candidates(ir,plan,raw,seed)
            apply(local,budget,'task_refine')
    apply(component+extra,budget,'partition_fallback')
    s=dict(case=case,problem=1,num_cores=cores,variant='portfolio_diverse' if structural_diversity else 'portfolio',budget=budget,best=best,
           best_record=best['record'] if best else None,evaluations=calls,skipped=skipped,routing=events,
           logical_calls=len(calls),new_calls=sum(not t['record']['cache_hit'] for t in calls),
           elapsed_seconds=time.monotonic()-start,prefix_calls=prefix_calls,task_call_reserve=reserve,
           stop_reason='time_budget' if time.monotonic()>=deadline else 'budget_or_pool_exhausted',
           soft_time_budget=time_budget,evaluation_dir=str(evaluation_dir),scope='no historical incumbent; one shared call/time budget; proxy gates are heuristic')
    atomic_json(out/'summary.json',s);return s
