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
