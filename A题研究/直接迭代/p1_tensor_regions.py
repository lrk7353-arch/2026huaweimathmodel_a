"""Communication-aware convex regions: retain large tensors and fuse cheap joins.

Pure graph-derived candidate generation. Original tensors, costs and dependencies
are unchanged. SCC closure makes contractions acyclic; only official evaluation
can establish whether memory use and dynamic contention improve.
"""
from collections import defaultdict
from common_run import validate_plan
from p1_convex_regions import _scc_coarsen
from p1_selective import topological_order, block_views, _assign, task_lower_bound
from p1_boundary_lower_bound import boundary_ddr_lower_bound


def regions(ir, tensor_threshold=4096, cheap_cycles=64):
    if tensor_threshold < 1 or cheap_cycles < 0:
        raise ValueError('positive tensor threshold and nonnegative cheap cost required')
    parent={i:i for i in ir.compute_ids};size={i:1 for i in parent}
    def root(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]];i=parent[i]
        return i
    def join(a,b):
        a,b=root(a),root(b)
        if a!=b:
            if size[a]<size[b]:a,b=b,a
            parent[b]=a;size[a]+=size[b]
    producers=defaultdict(list);consumers=defaultdict(list)
    for e in ir.graph['edges']:
        if e['source'] in parent:producers[e['target']].append(e['source'])
        if e['target'] in parent:consumers[e['source']].append(e['target'])
    heavy_links=cheap_links=0
    for tid,t in ir.tensors.items():
        for a in producers[tid]:
            for b in consumers[tid]:
                if t['size']>=tensor_threshold:
                    join(a,b);heavy_links+=1
                elif max(1,ir.ops[a]['cycles'],ir.ops[b]['cycles'])<=cheap_cycles:
                    join(a,b);cheap_links+=1
    order=topological_order(ir,'stable_id');groups=defaultdict(list)
    for i in order:groups[root(i)].append(i)
    blocks,diag=_scc_coarsen(ir,list(groups.values()),order)
    return blocks,dict(tensor_threshold=tensor_threshold,cheap_cycles=cheap_cycles,
                       heavy_links=heavy_links,cheap_links=cheap_links,
                       initial_groups=diag['initial_group_count'],tasks=len(blocks),
                       scc_groups_eliminated=diag['groups_eliminated'])


def generate(ir, num_cores=5):
    if num_cores not in range(1,6):raise ValueError('1..5 cores required')
    from p1_adaptive import plan_key
    candidates=[];seen=set()
    for threshold,cheap in ((4096,64),(4096,0),(16384,64),(4096,256)):
        blocks,diag=regions(ir,threshold,cheap);view=block_views(ir,blocks)
        for active in dict.fromkeys((num_cores,max(1,(num_cores+1)//2))):
            plan,proxy=_assign(ir,blocks,view,active,num_cores,'eft')
            sig=plan_key(plan)
            if sig in seen:continue
            seen.add(sig);validate_plan(ir,plan)
            tlb=task_lower_bound(ir,plan)['value'];dlb=boundary_ddr_lower_bound(ir,plan)
            candidates.append(dict(name=f'tensor{threshold}_cheap{cheap}_active{active}',plan=plan,
                metadata=dict(**diag,active_limit=active,task_bound=tlb,ddr_bound=dlb['lower_bound'],
                              lower_bound=max(tlb,dlb['lower_bound']),proxy=proxy,
                              mandatory_boundary_bytes=dlb['mandatory_boundary']['total_bytes'])))
    return candidates
