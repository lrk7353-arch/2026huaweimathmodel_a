"""Joint leader/follower and multiple-target P3 read-priority proposals.

Only existing compute work is reordered. No cache configuration, prefetch,
inserted delay or official simulator behavior is changed.
"""
from common_run import validate_plan
from advanced_solver.cache_refine import (_validate_observation, _observed_order_and_tails,
    _priority_order, _runs_plan, generate_cache_candidates)
import json


def generate(ir,plan,raw,cores,limit=24,round_index=0):
    view = _validate_observation(ir,plan,raw,cores)
    order,rank,_ = _observed_order_and_tails(ir,view)
    _,diag = generate_cache_candidates(ir,plan,raw,num_cores=cores,max_candidates=1,round_index=round_index,seed=17)
    assignment = view['core_by_op']
    local = {c:[o for o in order if assignment[o]==c] for c in range(cores)}
    ancestor_cache = {}
    def ancestors(o):
        if o not in ancestor_cache:
            reached,stack = set(),[o]
            while stack:
                n=stack.pop()
                if n not in reached: reached.add(n); stack.extend(ir.predecessors[n])
            ancestor_cache[o]=reached
        return ancestor_cache[o]
    def lift(priority,ops,anchor):
        for o in ops:
            for a in ancestors(o): priority[a]=min(priority.get(a,rank[a]),rank[anchor]-.25)
    # The earliest completed reader of a tensor is the current leader. Avoid
    # spending the whole pool trying to turn that compulsory first fill into a
    # hit; prioritize followers for which another core can actually supply it.
    leaders={tid:min(events,key=lambda e:(e['end'],e['time'],e['core_id']))
             for tid,events in view['groups'].items() if events}
    targets=[t for t in diag['ranked_targets'] if leaders[t['tensor_id']]['core_id']!=t['core_id']][:6]
    candidates,seen=[],{json.dumps(plan,separators=(',',':'))}
    def emit(name,priority,selected,width,mechanism):
        new_order=_priority_order(ir,rank,priority)
        new=_runs_plan(ir,new_order,assignment,cores)
        sig=json.dumps(new,separators=(',',':'))
        if sig in seen:return
        seen.add(sig);validate_plan(ir,new)
        candidates.append(dict(name=name,plan=new,metadata=dict(family='joint_reads',mechanism=mechanism,
            width=width,targets=selected,fixed_assignment=True,
            changed_positions=sum(a!=b for a,b in zip(order,new_order)))))
    emit('joint_read_encoding_control',{},[],0,'control')
    for width in ((2,8,32) if round_index%2==0 else (1,4,16)):
        bundles=[]
        for i,t in enumerate(targets):
            priority={};consumer=t['target_consumer'];core=t['core_id'];tid=t['tensor_id']
            loc=local[core];index=loc.index(consumer)
            leader=leaders[tid]
            lc=leader['core_id']
            readers=[o for o in view['consumers'][tid] if assignment[o]==lc and view['compute'][o]['start']>=leader['end']]
            if not readers:continue
            lead=min(readers,key=rank.__getitem__);lead_loc=local[lc]
            lift(priority,[lead],lead_loc[max(0,lead_loc.index(lead)-width)])
            if t['category']=='post_eviction_miss':
                lift(priority,[consumer],loc[max(0,index-width)])
                mechanism='leader_and_evicted_consumer'
            else:
                blocked=set(view['consumers'][tid]);useful=[]
                for op in loc[index+1:]:
                    if op in ancestors(consumer) or ancestors(op)&blocked:continue
                    useful.append(op)
                    if len(useful)>=width:break
                lift(priority,useful,consumer)
                mechanism='leader_and_independent_follower_work'
            emit(f'joint_read_r{round_index}_t{i}_w{width}',priority,[tid],width,mechanism)
            # A bundle spans distinct logical tensors; several followers of
            # one tensor remain useful individual proposals, not fake breadth.
            if tid not in {t for t,_ in bundles}:bundles.append((tid,priority))
        # Optimize several missed reuse opportunities in one feasible ordering.
        for count in (2,3):
            merged={}
            for _,p in bundles[:count]:
                for o,value in p.items():merged[o]=min(merged.get(o,rank[o]),value)
            if len(bundles)>=count:
                emit(f'joint_read_r{round_index}_bundle{count}_w{width}',merged,
                     [t for t,_ in bundles[:count]],width,'multiple_read_targets')
    # Give bundled changes slots even with many individual targets.
    controls=[c for c in candidates if c['metadata']['mechanism']=='control']
    bundles=[c for c in candidates if c['metadata']['mechanism']=='multiple_read_targets']
    singles=[c for c in candidates if c not in controls and c not in bundles]
    result=controls+[g[i] for i in range(max(len(bundles),len(singles))) for g in (bundles,singles) if i<len(g)]
    return result[:limit],dict(target_count=len(targets),generated=len(result),
        targets=[dict(tensor_id=t['tensor_id'],category=t['category'],priority_score=t['priority_score']) for t in targets],
        scope='joint reorder proposals; cache attribution requires paired controls; no guaranteed hits')
