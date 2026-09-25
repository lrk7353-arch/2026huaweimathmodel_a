"""Move connected compute regions, with optional load compensation and reordering.

Original route bytes and static Pipe work rank proposals; neither predicts the
official schedule, memory feasibility, DDR contention or P3 cache state.
"""
from collections import defaultdict
import heapq
import json

from common_run import validate_plan
from advanced_solver.trace_refine import (_plan_assignment, _tensor_views, _trace,
    _runs_plan, _topology, _traffic_delta, _priority_orders)


def affinity(ir, views):
    weights = defaultdict(lambda: defaultdict(int))
    for t, producers in views[0].items():
        for a in producers:
            for b in views[1][t]:
                weights[a][b] += ir.tensors[t]['size']
                weights[b][a] += ir.tensors[t]['size']
    return weights


def grow(ir, seed, assignment, weights, maximum, work_cap=float('inf')):
    """Connected same-core region, expanding strongest tensor links first."""
    core = assignment[seed]
    region, work, queued = [], defaultdict(int), {seed}
    heap = [(0, seed)]
    while heap and len(region) < maximum:
        _, o = heapq.heappop(heap)
        pipe, cost = ir.ops[o]['pipe'], max(1, ir.ops[o]['cycles'])
        if region and work[pipe] + cost > work_cap: continue
        region.append(o); work[pipe] += cost
        for n in sorted(set(ir.predecessors[o]) | set(ir.successors[o])):
            if n not in queued and assignment[n] == core:
                queued.add(n); heapq.heappush(heap, (-weights[o].get(n, 0), n))
    return region


def generate(ir, plan, raw, cores, limit=24, round_index=0):
    assignment = _plan_assignment(ir, plan, cores)
    views = _tensor_views(ir)
    timeline, routes, diag = _trace(ir, assignment, raw, cores, views, plan)
    order = _topology(ir, {o: (timeline[o]['start'], timeline[o]['end'], o) for o in ir.compute_ids})
    weights = affinity(ir, views)
    loads = [defaultdict(int) for _ in range(cores)]
    for o in ir.compute_ids: loads[assignment[o]][ir.ops[o]['pipe']] += max(1, ir.ops[o]['cycles'])
    peak = max((max(load.values(), default=0) for load in loads), default=0)
    late = sorted(routes, key=lambda r: (-r['copy_in_end'], -r['queue_wait'], r['tensor_id']))
    waiting = sorted(routes, key=lambda r: (-r['queue_wait'], -r['size'], r['tensor_id']))
    seeds = []
    for r in (late if round_index % 2 == 0 else waiting)[:6]:
        seeds.extend([('communication', r['producer'], r['target_core']),
                      ('communication', r['consumers'][0], r['source_core'])])
    hot = sorted(ir.compute_ids, key=lambda o: (-ir.ops[o]['cycles'], -timeline[o]['end'], o))[:8]
    for o in hot:
        targets = sorted((c for c in range(cores) if c != assignment[o]),
                         key=lambda c: (loads[c][ir.ops[o]['pipe']], c))[:2]
        seeds.extend(('load', o, c) for c in targets)
    proposals, move_seen = [], set()
    sizes = (8, 32, 128) if round_index % 2 == 0 else (16, 64, 256)
    def add(family, changes, seed, size, compensated=False):
        changes = {o: c for o, c in changes.items() if assignment[o] != c}
        ident = tuple(sorted(changes.items()))
        if not changes or ident in move_seen: return
        move_seen.add(ident)
        after = [dict(l) for l in loads]
        for o, c in changes.items():
            p, w = ir.ops[o]['pipe'], max(1, ir.ops[o]['cycles'])
            after[assignment[o]][p] -= w
            after[c][p] = after[c].get(p,0) + w
        new_peak = max(max(l.values(), default=0) for l in after)
        delta = _traffic_delta(ir, assignment, changes, views)
        proposals.append(dict(family=family, changes=changes, region_seed=seed, size_cap=size,
            moved_ops=len(changes), compensated=compensated, static_peak_change=new_peak-peak,
            structural_traffic_change=delta, proxy_delta=new_peak-peak+delta/60))
    for family, seed, destination in seeds:
        for size in sizes:
            moving = grow(ir, seed, assignment, weights, size, max(1, peak/2))
            changes = {o: destination for o in moving}
            add(family, changes, seed, size)
            # Exchange a smaller destination region as a separate joint action.
            source = assignment[seed]
            pipe = ir.ops[seed]['pipe']
            moved_work = sum(max(1,ir.ops[o]['cycles']) for o in moving if ir.ops[o]['pipe']==pipe)
            excess = max(0, (loads[destination][pipe]+moved_work-loads[source][pipe]+moved_work)/2)
            dest_seeds = [o for o in hot if assignment[o]==destination and ir.ops[o]['pipe']==pipe and o not in moving]
            if excess and dest_seeds:
                other = min(dest_seeds, key=lambda o: abs(ir.ops[o]['cycles']-excess))
                compensation = grow(ir, other, assignment, weights, size, excess)
                add('exchange', {**changes, **{o:source for o in compensation}}, seed, size, True)
    groups = defaultdict(list)
    for proposal in proposals: groups[proposal['family']].append(proposal)
    for group in groups.values(): group.sort(key=lambda x:(x['proxy_delta'],x['moved_ops'],x['region_seed']))
    ranked = [g[i] for i in range(max(map(len,groups.values()),default=0)) for g in groups.values() if i<len(g)]
    candidates, seen = [], {json.dumps(plan,separators=(',',':'))}
    def emit(name, value, metadata):
        sig = json.dumps(value,separators=(',',':'))
        if sig in seen: return
        validate_plan(ir,value); seen.add(sig)
        candidates.append(dict(name=name,plan=value,metadata=metadata))
    # Explicit encoding control: migration effects must not inherit credit for it.
    emit('region_observed_order_control',_runs_plan(ir,order,assignment,cores,max_run_ops=1),
         dict(family='control',moved_ops=0))
    for proposal in ranked[:max(8,limit)]:
        changed = dict(assignment); changed.update(proposal['changes'])
        meta = {k:v for k,v in proposal.items() if k!='changes'}
        meta['changes'] = [dict(op=o,from_core=assignment[o],to_core=c) for o,c in sorted(proposal['changes'].items())]
        # Compare preserved observed priority and dependency-tail priority.
        for ordering,new_order in [('observed',order),('tail',_priority_orders(ir,changed,1)[0][1])]:
            value = _runs_plan(ir,new_order,changed,cores,max_run_ops=1)
            emit(f'region_r{round_index}_{len(candidates)}_{ordering}',value,dict(**meta,ordering=ordering))
            if len(candidates)>=limit: break
        if len(candidates)>=limit: break
    diag.update(proposal_count=len(proposals),candidate_count=len(candidates),
                ranking='static pipe balance plus direct route bytes; heuristic only')
    return candidates,diag
