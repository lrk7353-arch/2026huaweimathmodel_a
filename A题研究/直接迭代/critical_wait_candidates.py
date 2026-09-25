"""Joint Task, placement, lifetime and FIFO-window candidates.

Only the supplied graph and paid parent observation are used. Proxies rank
complete legal plans; neither predicted hits nor timings certify improvement.
"""
from collections import Counter, defaultdict
import heapq
import json
import time

from common_run import validate_plan
from advanced_solver.trace_refine import _tensor_views, _runs_plan, _topology, _plan_assignment
from p23_data_refine import partition_copy_bytes, reuse_proposals
from wait_observation import observe


def signature(plan):
    return json.dumps(plan,ensure_ascii=False,separators=(',',':'))


def pipeline_order(ir, assignment, observed, observation, capacity, weight=1., window=48):
    """Ready-list heuristic combining pipe time, residency and observed urgency.

Remote reads use nominal transfer estimates. Predicted peaks and finishes are
not substitutes for the official spill, shared DDR or FIFO simulation.
"""
    views=_tensor_views(ir);rank={o:i for i,o in enumerate(observed)}
    remaining=Counter((t,assignment[o]) for t,readers in views[1].items() for o in readers)
    alive=set();live=Counter();peak=Counter();ends={};pipes=Counter();order=[]
    degree={o:len(ir.predecessors[o]) for o in ir.compute_ids}
    ready=[(rank[o],o) for o in ir.compute_ids if not degree[o]];heapq.heapify(ready)
    tail={o:observation['tails'][observation['compute'][o]] for o in ir.compute_ids}
    def effects(o):
        core=assignment[o];alloc=Counter();release=Counter()
        for t in views[2][o]:
            pos=ir.tensors[t]['pos'];pos='UB' if pos=='DDR' else pos
            if (t,core) not in alive:alloc[pos]+=ir.tensors[t]['size']
            if remaining[t,core]-int(o in views[1][t])==0:release[pos]+=ir.tensors[t]['size']
        return alloc,release
    while ready:
        choices=[heapq.heappop(ready) for _ in range(min(window,len(ready)))];scores={}
        for r,o in choices:
            core=assignment[o];release=0
            for p in ir.predecessors[o]:
                crossing=assignment[p]!=core
                move=sum(ir.tensors[t]['size'] for t in views[2][p]&views[2][o])/60 if crossing else 0
                release=max(release,ends[p]+(500+move if crossing else 0))
            root_bytes=sum(ir.tensors[t]['size'] for t in views[2][o]
                if not views[0][t] and (t,core) not in alive)
            start=max(release,pipes[core,ir.ops[o]['pipe']])+root_bytes/60
            alloc,freed=effects(o)
            pressure=sum(alloc[p]-freed[p]+2*max(0,live[core,p]+alloc[p]-capacity[p]) for p in capacity)/60
            scores[o]=(start-.35*tail[o]+weight*pressure,r,o,start,alloc,freed)
        chosen=min(choices,key=lambda x:scores[x[1]][:3]);o=chosen[1];core=assignment[o]
        _,_,_,start,alloc,freed=scores[o]
        for item in choices:
            if item!=chosen:heapq.heappush(ready,item)
        for p in capacity:
            peak[core,p]=max(peak[core,p],live[core,p]+alloc[p]);live[core,p]+=alloc[p]-freed[p]
            assert live[core,p]>=0
        for t in views[2][o]:
            key=t,core
            if o in views[1][t]:remaining[key]-=1
            if remaining[key]:alive.add(key)
            else:alive.discard(key)
        ends[o]=start+max(1,ir.ops[o]['cycles']);pipes[core,ir.ops[o]['pipe']]=ends[o];order.append(o)
        for n in ir.successors[o]:
            degree[n]-=1
            if degree[n]==0:heapq.heappush(ready,(rank[n],n))
    assert len(order)==len(ir.compute_ids)
    return order,dict(estimated_finish=max(ends.values(),default=0),surrogate_peak={f'{c}:{p}':v for (c,p),v in peak.items()})


def p1_candidates(ir,plan,raw,obs,cores,round_index,deadline):
    from p1_task_refine import view
    from p1_selective import topological_order
    from p1_bottleneck_repartition import partition_region,prepare,schedule,merge_region,tensor_affinity
    from p1_boundary_lower_bound import boundary_ddr_lower_bound
    mapping,owner,nodes,edges,preds=view(ir,plan);task_pressure=Counter()
    for key,c in obs['copies'].items():task_pressure[key[0]]+=c['wait_priority']
    chain_tasks=set(k[0] for k in obs['chain'])
    task_ends={t['task_id']:t for c in raw['per_core_timeline'] for t in c['tasks']}
    ranked=sorted(nodes,key=lambda t:(t not in chain_tasks,-task_pressure[t],-task_ends[t]['duration'],t))
    selected=[];views=_tensor_views(ir)
    for seed in ranked[:2]:
        region={seed};count=len(nodes[seed])
        if count>8192:continue
        roots={t for o in nodes[seed] for t in views[2][o] if not views[0][t]}
        shared=Counter()
        for t in roots:
            for o in views[1][t]:shared[mapping[o]]+=ir.tensors[t]['size']
        neighbours=edges[seed]|preds[seed]|set(shared)
        for t in sorted(neighbours-{seed},key=lambda t:(-task_pressure[t]-shared[t]/60,t)):
            if count+len(nodes[t])<=8192:region.add(t);count+=len(nodes[t])
            if len(region)>=6:break
        if sorted(region) not in selected:selected.append(sorted(region))
    order=topological_order(ir,'stable_id');affinity=tensor_affinity(ir);out=[];rejected=[]
    for ri,region in enumerate(selected):
        members={o for t in region for o in nodes[t]}
        total=max(sum(max(1,ir.ops[o]['cycles']) for o in members if ir.ops[o]['pipe']==p) for p in ('PIPE_M','PIPE_V'))
        for scale in (1,2,4):
            target=max(1,total/cores/scale)
            for family in ('branch','affinity'):
                try:
                    if time.monotonic()>=deadline:return out,dict(selected_regions=selected,rejected=rejected,partial=True)
                    remaining=max(.001,deadline-time.monotonic())
                    construction_deadline=time.perf_counter()+remaining
                    replacement=partition_region(ir,members,target,family,order,affinity,construction_deadline)
                    blocks,bv,fixed,pred=prepare(ir,plan,region,replacement,order)
                    for width in (1,4):
                        new,proxy=schedule(ir,plan,blocks,bv,fixed,pred,width,construction_deadline)
                        variants=[('raw',new),('merge',merge_region(ir,new,set(ir.compute_ids)-members,target,construction_deadline))]
                        for merge,value in variants:
                            d=boundary_ddr_lower_bound(ir,value)['mandatory_boundary']['total_bytes']
                            assigned=_plan_assignment(ir,value,cores)
                            meta=dict(family='wait_p1_'+family,old_tasks=region,region_ops=len(members),scale=scale,width=width,
                                merge=merge,proxy_finish=proxy,partition_copy_bytes=d,
                                changed_old_boundaries=sum(len({mapping[o] for o in b})>1 for b in replacement),
                                observed_region_copy_pressure=sum(task_pressure[t] for t in region),
                                assignment_changes=sum(assigned[o]!=owner[mapping[o]] for o in members))
                            out.append(dict(name=f'wait_p1_r{round_index}_{ri}_{family}_{scale}_{width}_{merge}',plan=value,metadata=meta))
                except (ValueError,TimeoutError) as e:rejected.append(str(e))
    return out,dict(selected_regions=selected,rejected=rejected)


def p23_candidates(ir,plan,raw,obs,cores,round_index,deadline):
    from p23_region_refine import grow,affinity
    assignment=obs['assignment'];views=_tensor_views(ir);weights=affinity(ir,views)
    order=_topology(ir,{o:(obs['events'][obs['compute'][o]]['start'],o) for o in ir.compute_ids})
    loads=Counter()
    for o in ir.compute_ids:loads[assignment[o],ir.ops[o]['pipe']]+=max(1,ir.ops[o]['cycles'])
    pressure=Counter();op_pressure=Counter()
    for key,e in obs['copies'].items():
        for t in e['original_tensors']:pressure[t]+=e['wait_priority']
    for o in ir.compute_ids:
        op_pressure[o]=max(1,ir.ops[o]['cycles'])/(1+obs['slack'][obs['compute'][o]]/max(1,.03*raw['makespan']))
    hot=sorted(ir.compute_ids,key=lambda o:(-op_pressure[o],o))[:8]
    proposals=[];seen=set()
    def add(changes,family,target=None):
        changes={o:c for o,c in changes.items() if c!=assignment[o]};sig=tuple(sorted(changes.items()))
        if not sig or sig in seen:return
        seen.add(sig);proposals.append(dict(changes=changes,family=family,target_tensor=target,
            pressure=pressure.get(target,0)+sum(op_pressure[o] for o in changes)))
    for t in sorted(pressure,key=lambda t:(-pressure[t],t))[:8]:
        grouped=defaultdict(list)
        for o in sorted(views[1][t]):grouped[assignment[o]].append(o)
        destinations=sorted(set(grouped)|{assignment[o] for o in views[0][t]})
        for src,ops in sorted(grouped.items()):
            for dst in destinations:
                if src==dst:continue
                add({o:dst for o in ops},'wait_readers',t);region=set(ops)
                for o in ops[:8]:region.update(grow(ir,o,assignment,weights,8 if round_index==0 else 24))
                if len(region)<=512:add({o:dst for o in region},'wait_reader_region',t)
    for o in hot[:6]:
        src=assignment[o];pipe=ir.ops[o]['pipe']
        for dst in sorted((c for c in range(cores) if c!=src),key=lambda c:(loads[c,pipe],c))[:2]:
            region=grow(ir,o,assignment,weights,8 if round_index==0 else 32,max(1,loads[src,pipe]/4))
            add({a:dst for a in region},'wait_load')
            other=[a for a in hot if assignment[a]==dst and ir.ops[a]['pipe']==pipe]
            if other:
                r=grow(ir,other[-1],assignment,weights,4,max(1,sum(ir.ops[a]['cycles'] for a in region)/2))
                add({**{a:dst for a in region},**{a:src for a in r}},'wait_exchange')
    for p in reuse_proposals(ir,assignment,{},cores,round_index)[:12]:add(p['changes'],'wait_'+p['family'],p['tensor_id'])
    def movement_priority(p):
        after=assignment.copy();after.update(p['changes']);newloads=loads.copy()
        for o,c in p['changes'].items():
            pipe=ir.ops[o]['pipe'];w=max(1,ir.ops[o]['cycles']);newloads[assignment[o],pipe]-=w;newloads[c,pipe]+=w
        p['copy_bytes']=partition_copy_bytes(ir,after,views);p['work_bound']=max(newloads.values(),default=0)
        return p['work_bound']+p['copy_bytes']/60/cores-.25*p['pressure']
    proposals.sort(key=lambda p:(movement_priority(p),p['family'],sorted(p['changes'].items())))
    candidates=[]
    def emit(name,owners,neworder,meta):
        value=_runs_plan(ir,neworder,owners,cores,max_run_ops=1);validate_plan(ir,value)
        candidates.append(dict(name=name,plan=value,metadata=meta))
    basebytes=partition_copy_bytes(ir,assignment,views)
    for weight in (.25,1.):
        neworder,info=pipeline_order(ir,assignment,order,obs,raw['capacity_bytes'],weight)
        emit(f'wait_lifetime_{weight}',assignment,neworder,dict(family='wait_lifetime',weight=weight,
            partition_copy_bytes=basebytes,moved_ops=0,**info))
    for i,p in enumerate(proposals[:12]):
        if time.monotonic()>=deadline-3:break
        owners=assignment.copy();owners.update(p['changes'])
        meta=dict(family=p['family'],target_tensor=p['target_tensor'],moved_ops=len(p['changes']),
            changes=[dict(op=o,from_core=assignment[o],to_core=c) for o,c in sorted(p['changes'].items())],
            partition_copy_bytes=p['copy_bytes'],fixed_work_bound=p['work_bound'],wait_pressure=p['pressure'])
        emit(f'wait_move_{i}',owners,order,dict(meta,ordering='observed'))
        neworder,info=pipeline_order(ir,owners,order,obs,raw['capacity_bytes'],.5)
        emit(f'wait_joint_{i}',owners,neworder,dict(meta,ordering='pipeline_lifetime',**info))
    if raw.get('problem')==3:
        from advanced_solver.cache_refine import _validate_observation
        from p3_joint_reads import generate as reads
        cv=_validate_observation(ir,plan,raw,cores);readplans,diag=reads(ir,plan,raw,cores,18,round_index)
        for i,c in enumerate(readplans):
            target=c['metadata'].get('targets',[])
            # Proxy uses potential reusable bytes; joint ranking uses trace wait.
            reuse_bytes=sum(ir.tensors[t]['size'] for t in target)
            c['metadata'].update(family='wait_fifo',wait_pressure=sum(pressure[t] for t in target),
                reusable_bytes=reuse_bytes,partition_copy_bytes=basebytes,ordering='fifo_window')
            c['name']='wait_'+c['name'];candidates.append(c)
            if i<6:
                readorder=sorted(ir.compute_ids,key=lambda o:c['plan']['node_to_subgraph'][str(o)])
                relevant=next((p for p in proposals if p['target_tensor'] in target),None)
                if relevant:
                    owners=assignment.copy();owners.update(relevant['changes'])
                    emit(f'wait_fifo_move_{i}',owners,readorder,dict(c['metadata'],family='wait_fifo_placement',
                        changes=[dict(op=o,from_core=assignment[o],to_core=v) for o,v in sorted(relevant['changes'].items())],
                        partition_copy_bytes=partition_copy_bytes(ir,owners,views)))
        cache_diag=dict(categories=dict(cv['counts']),candidate_diagnostics=diag)
    else:cache_diag=None
    return candidates,dict(placement_proposals=len(proposals),cache=cache_diag)


def generate(ir,plan,raw,cores,limit=24,round_index=0,policy='wait_joint',seconds=17):
    if policy not in ('wait_joint','proxy'):raise ValueError('unknown joint policy')
    deadline=time.monotonic()+seconds
    problem=raw.get('problem',1 if raw['scene']=='A' else 2);obs=observe(ir,plan,raw,problem)
    candidates,diag=(p1_candidates(ir,plan,raw,obs,cores,round_index,deadline) if problem==1 else
                     p23_candidates(ir,plan,raw,obs,cores,round_index,deadline))
    seen={signature(plan)};unique=[]
    for c in candidates:
        sig=signature(c['plan'])
        if sig not in seen:seen.add(sig);unique.append(c)
    if policy=='proxy':
        unique.sort(key=lambda c:(c['metadata'].get('partition_copy_bytes',float('inf')),
            -c['metadata'].get('reusable_bytes',0),c['name']))
    else:
        pools=defaultdict(list)
        for c in unique:pools[c['metadata']['family']].append(c)
        for group in pools.values():
            group.sort(key=lambda c:(c['metadata'].get('ordering')=='observed',
                c['metadata'].get('proxy_finish',c['metadata'].get('estimated_finish',0)),
                -c['metadata'].get('wait_pressure',0),c['name']))
        preferred=['wait_fifo_placement','wait_fifo','wait_lifetime','wait_readers','wait_reader_region','wait_exchange','wait_load']
        families=[f for f in preferred if f in pools]+[f for f in pools if f not in preferred]
        unique=[pools[f][i] for i in range(max(map(len,pools.values()),default=0)) for f in families if i<len(pools[f])]
    return unique[:limit],dict(observation=obs['summary'],generation=diag,
        generated=len(candidates),unique=len(unique),selected=min(limit,len(unique)),policy=policy)
