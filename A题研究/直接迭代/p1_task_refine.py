"""P1 Task neighborhoods; no evaluator calls and no graph-ID rules."""
from collections import defaultdict,Counter
import heapq,random
from common_run import validate_plan
from p1_selective import task_lower_bound,topological_order
from p1_boundary_lower_bound import boundary_ddr_lower_bound

def view(ir,plan,core_edges=False):
    mapping={int(o):s for o,s in plan['node_to_subgraph'].items()}
    owner={s:c for c,seq in enumerate(plan['core_schedules']) for s in seq}
    nodes={s:[] for s in owner}
    for o,s in mapping.items():nodes[s].append(o)
    edges={s:set() for s in owner}
    for o,children in ir.successors.items():
        for child in children:
            if mapping[o]!=mapping[child]:edges[mapping[o]].add(mapping[child])
    if core_edges:
        for seq in plan['core_schedules']:
            for a,b in zip(seq,seq[1:]):edges[a].add(b)
    pred={s:set() for s in owner}
    for a,children in edges.items():
        for b in children:pred[b].add(a)
    return mapping,owner,nodes,edges,pred

def coalesce(ir,plan,max_ops,raw=None,seed=17):
    """Merge consecutive same-core Tasks in a topological order of data+core DAG.

    Every group is an interval of a topological order, hence its contraction
    is acyclic. No operation changes core. Existing oversized Tasks are kept.
    """
    mapping,owner,nodes,edges,pred=view(ir,plan,True)
    start={e['task_id']:e['start'] for c in (raw or {}).get('per_core_timeline',[]) for e in c['tasks']}
    rng=random.Random(seed);tie={s:rng.random() for s in sorted(owner)}
    degree={s:len(pred[s]) for s in owner};ready={s for s in owner if degree[s]==0}
    groups=[];block=[];size=0;core=None
    while ready:
        continuation=[s for s in ready if owner[s]==core and size+len(nodes[s])<=max_ops]
        if not continuation:
            if block:groups.append(block)
            block=[];size=0;core=None
        pool=continuation or ready
        s=min(pool,key=lambda s:(start.get(s,0),tie[s],s));ready.remove(s)
        block.append(s);size+=len(nodes[s]);core=owner[s]
        for child in edges[s]:
            degree[child]-=1
            if degree[child]==0:ready.add(child)
    if block:groups.append(block)
    if sum(map(len,groups))!=len(owner):raise ValueError('cyclic incumbent')
    representative={s:g[0] for g in groups for s in g}
    schedules=[]
    for seq in plan['core_schedules']:
        merged=[]
        for s in seq:
            if not merged or merged[-1]!=representative[s]:merged.append(representative[s])
        schedules.append(merged)
    result={'node_to_subgraph':{o:representative[s] for o,s in plan['node_to_subgraph'].items()},'core_schedules':schedules}
    validate_plan(ir,result)
    return result

def split_large(ir,plan,max_ops):
    """Split large incumbent Tasks, keeping core ownership and topological order."""
    mapping,owner,nodes,_,_=view(ir,plan)
    by_task=defaultdict(lambda:defaultdict(list))
    for o in topological_order(ir,'stable_id'):by_task[mapping[o]][ir.component_by_op[o]].append(o)
    assigned={};replacement={};next_id=max(owner,default=-1)+1
    for sg in owner:
        blocks=[];bucket=[]
        for members in by_task[sg].values():
            if len(members)>max_ops:
                if bucket:blocks.append(bucket);bucket=[]
                blocks.extend(members[i:i+max_ops] for i in range(0,len(members),max_ops))
            else:
                if len(bucket)+len(members)>max_ops:blocks.append(bucket);bucket=[]
                bucket+=members
        if bucket:blocks.append(bucket)
        replacement[sg]=[]
        for i,block in enumerate(blocks):
            ident=sg if i==0 else next_id
            if i:next_id+=1
            replacement[sg].append(ident)
            for op in block:assigned[op]=ident
    result={'node_to_subgraph':{o:assigned[int(o)] for o in plan['node_to_subgraph']},
            'core_schedules':[[n for s in seq for n in replacement[s]] for seq in plan['core_schedules']]}
    validate_plan(ir,result);return result

def reschedule(ir,plan,raw,metric,seed=17):
    """Same partition; list-schedule with measured local or contended duration."""
    mapping,owner,nodes,edges,pred=view(ir,plan)
    if metric=='local':duration={int(s):r['local_makespan'] for s,r in raw['step3_by_task'].items()}
    else:duration={r['task_id']:r['duration'] for c in raw['per_core_timeline'] for r in c['tasks']}
    degree={s:len(pred[s]) for s in owner};q=[s for s in owner if degree[s]==0];heapq.heapify(q);order=[]
    while q:
        s=heapq.heappop(q);order.append(s)
        for c in edges[s]:
            degree[c]-=1
            if not degree[c]:heapq.heappush(q,c)
    if len(order)!=len(owner):raise ValueError('cyclic Task data DAG')
    tail={}
    for s in reversed(order):tail[s]=duration[s]+max((tail[c] for c in edges[s]),default=0)
    rng=random.Random(seed);tie={s:rng.random() for s in sorted(owner)}
    degree={s:len(pred[s]) for s in owner};q=[(-tail[s],tie[s],s) for s in owner if not degree[s]];heapq.heapify(q)
    cores=len(plan['core_schedules']);seq=[[] for _ in range(cores)];avail=[0]*cores;ends={};assigned={}
    while q:
        _,_,s=heapq.heappop(q)
        def finish(c):
            release=max((ends[p]+(1000 if assigned[p]!=c else 0) for p in pred[s]),default=0)
            return max(release,avail[c]+(100 if seq[c] else 0))+duration[s]
        c=min(range(cores),key=lambda c:(finish(c),len(seq[c]),c))
        ends[s]=avail[c]=finish(c);assigned[s]=c;seq[c].append(s)
        for child in edges[s]:
            degree[child]-=1
            if not degree[child]:heapq.heappush(q,(-tail[child],tie[child],child))
    result={'node_to_subgraph':dict(plan['node_to_subgraph']),'core_schedules':seq}
    validate_plan(ir,result);return result

def generate(ir,plan,raw,seed=17,merge_caps=None):
    candidates=[]
    specs=[('merge256',lambda:coalesce(ir,plan,256,raw,seed)),
           ('local_duration',lambda:reschedule(ir,plan,raw,'local',seed)),
           ('merge512',lambda:coalesce(ir,plan,512,raw,seed)),
           ('observed_duration',lambda:reschedule(ir,plan,raw,'observed',seed)),
           ('merge1024',lambda:coalesce(ir,plan,1024,raw,seed)),
           ('split1024',lambda:split_large(ir,plan,1024)),
           ('merge2048',lambda:coalesce(ir,plan,2048,raw,seed)),
           ('split512',lambda:split_large(ir,plan,512))]
    if merge_caps is not None:
        if not merge_caps or any(type(c) is not int or c<1 for c in merge_caps):raise ValueError('positive merge caps required')
        specs=[(f'merge{cap}',lambda cap=cap:coalesce(ir,plan,cap,raw,seed)) for cap in dict.fromkeys(merge_caps)]
    for name,make in specs:
        value=make();counts=Counter(value['node_to_subgraph'].values())
        tlb=task_lower_bound(ir,value)['value'];ddr=boundary_ddr_lower_bound(ir,value)
        candidates.append({'name':name,'plan':value,'metadata':{
            'task_bound':tlb,'ddr_bound':ddr['lower_bound'],'lower_bound':max(tlb,ddr['lower_bound']),
            'task_count':len(counts),'max_task_ops':max(counts.values()),
            'mandatory_boundary_bytes':ddr['mandatory_boundary']['total_bytes'],
            'core_ownership_preserved':name.startswith(('merge','split'))}})
    return candidates
