"""Two bounded, opt-in attacks on strong incumbents; no graph-ID dispatch.

Ownership search deliberately crosses old Task boundaries and accepts temporary
proxy regressions. Proxies never certify official performance or FIFO hits.
"""
from collections import defaultdict
import heapq
import math
import random
import time

from common_run import validate_plan
from unified_structure import owner_map


def pack_ownership(s, owners, cores, scale, ordering='critical'):
    """Contract only an available same-owner frontier, keeping quotient acyclic."""
    ir = s.ir
    degree = {o: len(ir.predecessors[o]) for o in s.topo}
    ready = {o for o in s.topo if not degree[o]}
    target = max(1000, s.profile['compute_floor_work'] / (cores * scale))
    mapping, schedules = {}, [[] for _ in range(cores)]
    def priority(o):
        release = sum(s.tensors[t].size for t in s.inputs[o]) - sum(s.tensors[t].size for t in s.outputs[o])
        return (-(s.tail[o] + (release / 60 if ordering == 'release' else 0)), o)
    queue = [priority(o) for o in ready]
    heapq.heapify(queue)
    sg = 0
    while ready:
        while queue[0][-1] not in ready:
            heapq.heappop(queue)
        root = heapq.heappop(queue)[-1]
        core, options, work, count = owners[root], [priority(root)], [0, 0], 0
        schedules[core].append(sg)
        while options:
            o = heapq.heappop(options)[-1]
            if o not in ready:
                continue
            pipe = int(ir.ops[o]['pipe'] != 'PIPE_M')
            if count and (work[pipe] + ir.ops[o]['cycles'] > target or count >= 512):
                continue
            ready.remove(o)
            mapping[str(o)] = sg
            work[pipe] += ir.ops[o]['cycles']
            count += 1
            for child in ir.successors[o]:
                degree[child] -= 1
                if not degree[child]:
                    ready.add(child)
                    heapq.heappush(queue, priority(child))
                    if owners[child] == core:
                        heapq.heappush(options, priority(child))
        sg += 1
    plan = dict(node_to_subgraph=mapping, core_schedules=schedules)
    validate_plan(ir, plan)
    return plan


def reverse_ownership(s, cores, weight):
    """Schedule backwards from sinks, then use only the resulting ownership."""
    ir = s.ir
    degree = {o: len(ir.successors[o]) for o in s.topo}
    q = [(-s.earliest[o], o) for o in s.topo if not degree[o]]
    heapq.heapify(q)
    clocks = [[0., 0.] for _ in range(cores)]
    owner, finish = {}, {}
    while q:
        _, o = heapq.heappop(q)
        pipe = int(ir.ops[o]['pipe'] != 'PIPE_M')
        options = []
        for c in range(cores):
            release = max((finish[n] + (weight * (500 + s.affinity[o, n] / 30) if owner[n] != c else 0)
                           for n in ir.successors[o]), default=0)
            end = max(release, clocks[c][pipe]) + ir.ops[o]['cycles']
            options.append((end, sum(clocks[c]), c))
        end, _, core = min(options)
        owner[o], finish[o], clocks[core][pipe] = core, end, end
        for p in ir.predecessors[o]:
            degree[p] -= 1
            if not degree[p]:
                heapq.heappush(q, (-s.earliest[p], p))
    return owner


def anneal_ownership(s, original, cores, scene, weight, seed, deadline):
    """Tensor-net cut refinement with bulk moves, swaps and nonmonotone escape."""
    rng = random.Random(seed)
    ir, owners = s.ir, dict(original)
    units = s.regions(cores, 16, 'affinity', deadline) + s.regions(cores, 8, 'branch', deadline)
    work = [[0., 0.] for _ in range(cores)]
    for o in s.topo:
        work[owners[o]][int(ir.ops[o]['pipe'] != 'PIPE_M')] += ir.ops[o]['cycles']
    def netcost(tid, changed=None):
        t = s.tensors[tid]
        def own(o):
            return changed.get(o, owners[o]) if changed else owners[o]
        src, dst = {own(o) for o in t.producers}, {own(o) for o in t.consumers}
        routes = sum(a != b for a in src for b in dst)
        reads = len(dst) if not src else routes
        # Repeated-read discount is explicitly a ranking proxy, not a hit claim.
        discount = .35 * max(0, reads-1) if scene == 3 and t.size <= 1048576 else 0
        return ((reads + routes - discount) * t.size / 60 + routes * 500) / cores
    costs = {t: netcost(t) for t in s.tensors}
    traffic = sum(costs.values())
    objective = max(max(w) for w in work) + weight * traffic
    best_value, best, accepted = objective, dict(owners), 0
    for step in range(192):
        if time.monotonic() >= deadline:
            break
        unit = units[rng.randrange(len(units))]
        target = rng.randrange(cores)
        change = {o: target for o in unit if owners[o] != target}
        if step % 4 == 0:
            other = units[rng.randrange(len(units))]
            source = owners[unit[0]]
            change.update({o: source for o in other if o not in change and owners[o] != source})
        if not change:
            continue
        new_work = [w.copy() for w in work]
        affected = set()
        for o, c in change.items():
            p, duration = int(ir.ops[o]['pipe'] != 'PIPE_M'), ir.ops[o]['cycles']
            new_work[owners[o]][p] -= duration
            new_work[c][p] += duration
            affected.update(s.inputs[o]); affected.update(s.outputs[o])
        updated = {t: netcost(t, change) for t in affected}
        new_traffic = traffic + sum(updated[t] - costs[t] for t in affected)
        candidate = max(max(w) for w in new_work) + weight * new_traffic
        temperature = max(1., best_value * .035 * (1-step/192)**2)
        if candidate < objective or rng.random() < math.exp(min(0, (objective-candidate)/temperature)):
            owners.update(change); work = new_work; traffic = new_traffic
            costs.update(updated); objective = candidate; accepted += 1
            if objective < best_value:
                best_value, best = objective, dict(owners)
    return best, dict(proxy=best_value, accepted_proxy_moves=accepted)


def round_one(s, scene, cores, incumbent, deadline):
    parent = owner_map(incumbent)
    # Every candidate rewrites the full ownership/partition, not only old Tasks.
    specs = [(False, .03, 2), (True, .1, 4), (False, .2, 8), (True, .5, 16),
             (False, 1., 4), (True, 2., 8), (False, 4., 16), (True, .02, 2)]
    for i, (reverse, weight, scale) in enumerate(specs):
        if time.monotonic() >= deadline:
            return
        owners = reverse_ownership(s, cores, weight) if reverse else parent
        owners, meta = anneal_ownership(s, owners, cores, scene, weight, 1700+i,
                                       min(deadline, time.monotonic()+5))
        plan = pack_ownership(s, owners, cores, scale, 'release' if scene > 1 else 'critical')
        yield dict(name=f'owner_first_r{i}_w{weight}_s{scale}', plan=plan,
                   metadata=dict(family='global_tensor_ownership', reverse_start=reverse,
                     weight=weight, scale=scale, moved_ops=sum(owners[o]!=parent[o] for o in owners), **meta))


def preserve_encoding(s, original, owners, cores):
    """Retain unaffected SGs and all old per-core relative SG priorities.

    Changed SGs alone are split into runs in an internal compute topology. A
    topological order of old SG dependencies plus core order is a safe scaffold;
    original dictionary insertion order is retained exactly.
    """
    old = {int(o):t for o,t in original['node_to_subgraph'].items()}
    blocks=defaultdict(list)
    for o in s.topo: blocks[old[o]].append(o)
    pred={t:set() for t in blocks};succ={t:set() for t in blocks}
    for o in s.topo:
        for p in s.ir.predecessors[o]:
            if old[p]!=old[o]:pred[old[o]].add(old[p])
    for seq in original['core_schedules']:
        for a,b in zip(seq,seq[1:]):pred[b].add(a)
    for b,ps in pred.items():
        for a in ps:succ[a].add(b)
    degree={t:len(ps) for t,ps in pred.items()}
    ready=[t for t,d in degree.items() if not d];heapq.heapify(ready)
    mapping,schedules,sg={},[[] for _ in range(cores)],0
    while ready:
        t=heapq.heappop(ready);previous=None
        for o in blocks[t]:
            c=owners[o]
            if c!=previous:
                schedules[c].append(sg);sg+=1;previous=c
            mapping[str(o)]=sg-1
        for child in succ[t]:
            degree[child]-=1
            if not degree[child]:heapq.heappush(ready,child)
    plan=dict(node_to_subgraph={o:mapping[o] for o in original['node_to_subgraph']},core_schedules=schedules)
    validate_plan(s.ir,plan)
    return plan


def timed_frontiers(s, owners, compute, cores, bands):
    """Transfer a donor's ownership/timing into legal, bounded P1 Task windows."""
    makespan=max(x['end'] for x in compute.values())
    bucket={o:min(bands-1,int(compute[o]['start']*bands/max(1,makespan))) for o in s.topo}
    degree={o:len(s.ir.predecessors[o]) for o in s.topo};ready=set()
    globalq=[];local=[[] for _ in range(cores)]
    def push(o):
        ready.add(o);key=(bucket[o],compute[o]['start'],o)
        heapq.heappush(globalq,key);heapq.heappush(local[owners[o]],key)
    for o in s.topo:
        if not degree[o]:push(o)
    mapping,schedules,sg={},[[] for _ in range(cores)],0
    while ready:
        while globalq[0][-1] not in ready:heapq.heappop(globalq)
        band,_,root=globalq[0];c=owners[root];count=0;schedules[c].append(sg)
        while local[c]:
            if local[c][0][-1] not in ready:heapq.heappop(local[c]);continue
            if local[c][0][0]!=band or count>=512:break
            _,_,o=heapq.heappop(local[c]);ready.remove(o);mapping[str(o)]=sg;count+=1
            for child in s.ir.successors[o]:
                degree[child]-=1
                if not degree[child]:push(child)
        sg+=1
    plan=dict(node_to_subgraph=mapping,core_schedules=schedules)
    validate_plan(s.ir,plan);return plan


def round_two(s, scene, cores, incumbent, record, deadline, donors=()):
    import gzip
    import json
    from common_run import read_json
    owners=owner_map(incumbent)
    with gzip.open(record['result_path'],'rt') as f:raw=json.load(f)
    compute={op['op_id']:op for c in raw['per_core_timeline'] for op in c['ops'] if op['op_id'] in owners}
    assert set(compute)==set(s.topo)
    ends=[max((compute[o]['end'] for o in s.topo if owners[o]==c),default=0) for c in range(cores)]
    heavy=max(range(cores),key=lambda c:ends[c]);light=min(range(cores),key=lambda c:ends[c])
    # Two cross-scene parents are explicit upstream inputs, never free target scores.
    for i,donor in enumerate(donors[:2]):
        if time.monotonic()>=deadline:return
        plan=read_json(donor['plan_path'])
        if scene==1:
            with gzip.open(donor['result_path'],'rt') as f:other=json.load(f)
            times={op['op_id']:op for c in other['per_core_timeline'] for op in c['ops'] if op['op_id'] in owners}
            plan=timed_frontiers(s,owner_map(plan),times,cores,4 if i==0 else 12)
        yield dict(name=f'trace_donor_p{donor["problem"]}',plan=plan,metadata=dict(family='cross_scene_timed_parent',
            source_record=donor['record_path'],source_plan_sha256=donor['hashes']['plan_sha256'],source_problem=donor['problem']))
    roots=sorted((o for o in s.topo if owners[o]==heavy),key=lambda o:(-(compute[o]['end']+.1*s.tail[o]),o))
    total=sum(s.ir.ops[o]['cycles'] for o in roots)
    def grow(root,limit,owner):
        q=[(-compute[root]['end'],root)];chosen=set();work=0
        while q and work<limit:
            _,o=heapq.heappop(q)
            if o in chosen or owners[o]!=owner:continue
            chosen.add(o);work+=s.ir.ops[o]['cycles']
            for n in s.ir.predecessors[o]+s.ir.successors[o]:
                if n not in chosen and owners[n]==owner:
                    heapq.heappush(q,(-compute[n]['end'],n))
        return chosen
    for i,fraction in enumerate((.08,.25,.45,.15)):
        if time.monotonic()>=deadline:return
        root=roots[0 if i<3 else min(len(roots)-1,len(roots)//4)]
        chosen=grow(root,max(1,total*fraction),heavy)
        target=light if light!=heavy else (heavy+1)%cores
        changed=dict(owners);changed.update({o:target for o in chosen})
        if i==3:
            other=sorted((o for o in s.topo if owners[o]==target),key=lambda o:(compute[o]['start'],o))
            if other:
                exchanged=grow(other[0],max(1,total*fraction*.5),target)
                changed.update({o:heavy for o in exchanged})
        plan=preserve_encoding(s,incumbent,changed,cores)
        yield dict(name=f'trace_bulk_{i}',plan=plan,metadata=dict(family='late_connected_region_exchange',
            moved_ops=sum(changed[o]!=owners[o] for o in owners),heavy_core=heavy,target_core=target,
            fraction=fraction,observed_ends=ends,exact_critical_path_claimed=False))
    # Joint producer/consumer co-location: use whole consumer sets, not one op.
    links=[]
    for tid,t in s.tensors.items():
        if t.producers and t.consumers and len({owners[o] for o in t.producers+t.consumers})>1:
            lateness=max(compute[o]['end'] for o in t.consumers)
            links.append((lateness+t.size/30,tid))
    for i,(_,tid) in enumerate(sorted(links,reverse=True)[:2]):
        if time.monotonic()>=deadline:return
        t=s.tensors[tid];changed=dict(owners)
        if i==0:
            target=owners[max(t.producers,key=lambda o:compute[o]['end'])]
            members=set(t.consumers)
        else:
            target=owners[max(t.consumers,key=lambda o:compute[o]['end'])]
            members=set(t.producers)
            for o in t.producers:members.update(s.ir.predecessors[o])
        changed.update({o:target for o in members})
        yield dict(name=f'trace_tensor_cone_{i}',plan=preserve_encoding(s,incumbent,changed,cores),
            metadata=dict(family='late_tensor_cone_colocation',tensor=tid,moved_ops=sum(changed[o]!=owners[o] for o in owners),
                          selected_tensor_bytes=t.size))
