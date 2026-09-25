"""Fixed-core P3 read-order neighbourhood, with an explicit encoding control.

Uses observed read timings to move existing useful work. No inserted delays,
prefetches or simulator changes. Read urgency and window sizes are heuristics.
All variants share the control's exact node_to_subgraph mapping; only schedules
change. The controller must evaluate every accepted variant officially.
"""
import gzip
from collections import defaultdict
from common_run import *
from advanced_solver.cache_refine import (_validate_observation,_observed_order_and_tails,
    _priority_order,_runs_plan,generate_cache_candidates)


def generate(ir,plan,raw,cores,limit=12):
    if not 1<=cores<=5 or limit<1:raise ValueError('positive limit and 1..5 cores required')
    view=_validate_observation(ir,plan,raw,cores)
    order,rank,_=_observed_order_and_tails(ir,view)
    _,diag=generate_cache_candidates(ir,plan,raw,num_cores=cores,max_candidates=1,seed=17)
    assignment=view['core_by_op'];control=_runs_plan(ir,order,assignment,cores)
    fixed_mapping=control['node_to_subgraph'];local={c:[o for o in order if assignment[o]==c] for c in range(cores)}
    ancestor_cache={}
    def ancestors(op):
        if op not in ancestor_cache:
            reached=set();stack=[op]
            while stack:
                n=stack.pop()
                if n not in reached:reached.add(n);stack.extend(ir.predecessors[n])
            ancestor_cache[op]=reached
        return ancestor_cache[op]
    def lift(ops,anchor):
        chosen=set().union(*(ancestors(o) for o in ops))
        return _priority_order(ir,rank,{o:min(rank[o],rank[anchor]-.25) for o in chosen})
    candidates=[];seen=set()
    def add(name,new_order,mechanism,target=None,**extra):
        schedules=[[] for _ in range(cores)]
        for op in new_order:schedules[assignment[op]].append(fixed_mapping[str(op)])
        identity=tuple(tuple(s) for s in schedules)
        if identity in seen:return
        candidate={'node_to_subgraph':dict(fixed_mapping),'core_schedules':schedules}
        validate_plan(ir,candidate);seen.add(identity)
        metadata=dict(mechanism=mechanism,is_reencoding_control=target is None,
                      fixed_op_to_core=True,fixed_control_mapping=True,
                      changed_core_sequences=sum(a!=b for a,b in zip(schedules,control['core_schedules'])),
                      target={k:target[k] for k in ('tensor_id','core_id','category','target_consumer','priority_score','time','end')} if target else None,
                      **extra)
        candidates.append(dict(name=name,plan=candidate,metadata=metadata))
    add('observed_order_control',order,'reencoding_control')
    # Separate mechanisms and scales so one target cannot consume the pool.
    targets=diag['ranked_targets'][:8]
    for width in (8,32,128):
        for i,target in enumerate(targets):
            tid,core,consumer=target['tensor_id'],target['core_id'],target['target_consumer']
            loc=local[core];index=loc.index(consumer);anchor=loc[max(0,index-width)]
            add(f't{i}_w{width}_advance',lift([consumer],anchor),'advance_critical_consumer',target,width=width)
            # Choose an observed earlier reader on another core, if any.
            others=[e for e in view['groups'][tid] if e['core_id']!=core and e['end']<=target['end']]
            if others:
                leader=min(others,key=lambda e:(e['end'],e['core_id'],e['op_id']))
                lead_core=leader['core_id'];lead_consumers=[o for o in view['consumers'][tid]
                    if assignment[o]==lead_core and view['compute'][o]['start']>=leader['end']]
                if lead_consumers:
                    lead=min(lead_consumers,key=lambda o:rank[o]);lead_loc=local[lead_core]
                    lead_anchor=lead_loc[max(0,lead_loc.index(lead)-width)]
                    add(f't{i}_w{width}_leader',lift([lead],lead_anchor),'advance_existing_leader',target,width=width,leader_core=lead_core)
                blocked=set(view['consumers'][tid]);selected=[]
                # Advancing useful independent work can stagger the follower;
                # there is no claimed guarantee of an actual Cache hit.
                for op in loc[index+1:]:
                    if op in ancestors(consumer) or ancestors(op)&blocked:continue
                    selected.append(op)
                    if len(selected)>=width:break
                if selected:
                    add(f't{i}_w{width}_work',lift(selected,consumer),'independent_work_before_follower',target,
                        width=width,warmup_ops=selected,leader_core=lead_core)
    groups=defaultdict(list)
    for c in candidates[1:]:groups[c['metadata']['mechanism'],c['metadata']['width']].append(c)
    ordered=[candidates[0]]+[group[i] for i in range(max(map(len,groups.values()),default=0)) for group in groups.values() if i<len(group)]
    candidates=ordered[:limit]
    diag.update(candidate_count=len(candidates),scope='fixed assignment and control mapping; observed timing heuristic; official makespan acceptance required')
    return candidates,diag


def run(case,old,out,budget=12,seconds=120,cores=5):
    if key(old)!=(case,3,cores):raise ValueError('incumbent case/problem/core mismatch')
    if budget<1 or seconds<=0:raise ValueError('positive budget/time required')
    out=Path(out);out.mkdir(parents=True,exist_ok=False);start=time.monotonic();deadline=start+seconds
    ir=GraphIR.from_path(DATA/(case+'.json'));input_plan=read_json(old['plan_path'])
    with gzip.open(old['result_path'],'rt') as f:raw=json.load(f)
    candidates,diag=generate(ir,input_plan,raw,cores,limit=budget)
    atomic_json(out/'diagnostics.json',diag);atomic_json(out/'candidates.json',[dict(name=c['name'],**c['metadata']) for c in candidates])
    best=old;trials=[]
    for c in candidates:
        if len(trials)>=budget or time.monotonic()>=deadline:break
        # Even an identical control is evaluated/reused and charged: mechanism
        # attribution needs its own official record, not an implicit baseline.
        rec=evaluate(DATA/(case+'.json'),c['plan'],3,R/'advanced_solver/runs/formal_v2/evaluations',
                     timeout=min(60,max(.1,deadline-time.monotonic())),config_path=DATA/'config.txt')
        accepted=rec['status']=='success' and score(rec)<score(best)
        trials.append(dict(name=c['name'],metadata=c['metadata'],record=rec,accepted=accepted))
        if accepted:best=rec
        atomic_json(out/'progress.json',dict(case=case,calls=len(trials),before=score(old)[0],after=score(best)[0]))
    atomic_json(out/'best.plan.json',read_json(best['plan_path']))
    s=dict(case=case,problem=3,num_cores=cores,before=score(old)[0],after=score(best)[0],best_record=best,
           calls=trials,logical_calls=len(trials),new_calls=sum(not t['record']['cache_hit'] for t in trials),
           budget=budget,elapsed_seconds=time.monotonic()-start,
           stop_reason='time_budget' if time.monotonic()>=deadline else 'budget_or_candidates',
           scope='extra-budget fixed-incumbent read-order refinement; no performance attribution without P2/P3 cross-evaluation')
    atomic_json(out/'summary.json',s);return s
