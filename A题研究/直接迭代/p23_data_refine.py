"""P2/P3 input reuse and bounded tensor-lifetime ordering neighborhoods.

The pre-spill COPY byte model matches the official scene-B partition rules.
Lifetime estimates omit COPY duration, physical address reuse and FIFO cache;
they rank proposals only. Every accepted plan needs an official evaluation.
"""
from collections import defaultdict
import heapq
import json

from common_run import validate_plan
from advanced_solver.trace_refine import (_tensor_views, _plan_assignment, _trace,
    _topology, _runs_plan)
from p23_region_refine import affinity, grow


def output_tensors(ir, views):
    copy_outputs = {e['source'] for e in ir.graph['edges']
                    if e['target'] in ir.ops and ir.ops[e['target']]['op'] == 'COPY_OUT'}
    return {t for t in ir.tensors if views[0][t] and (t in copy_outputs or not views[1][t])}


def tensor_copy_bytes(ir, t, assignment, views, outputs):
    sources = {assignment[o] for o in views[0][t]}
    targets = {assignment[o] for o in views[1][t]}
    reads = len(targets) if targets and not sources else 0
    writes = len(sources) if t in outputs else 0
    routes = sum(s != d for s in sources for d in targets)
    return ir.tensors[t]['size'] * (reads + writes + 2 * routes)


def partition_copy_bytes(ir, assignment, views=None):
    views = views or _tensor_views(ir)
    outputs = output_tensors(ir, views)
    return sum(tensor_copy_bytes(ir, t, assignment, views, outputs) for t in ir.tensors)


def lifetime_order(ir, assignment, observed, window=32, weight=1., capacity=None):
    """Topological list scheduling over a bounded ready frontier.

    Prefer consuming the last local use of a live tensor over opening more
    tensors. The observed priority provides a trust region, not a time bound.
    A remote COPY is approximated as immediate: reported peaks are surrogates.
    """
    if window < 1 or weight < 0: raise ValueError('invalid lifetime parameters')
    capacity = capacity or {'L1': 524288, 'UB': 131072}
    views = _tensor_views(ir); rank = {o: i for i, o in enumerate(observed)}
    if set(rank) != set(ir.compute_ids) or len(observed) != len(ir.compute_ids):
        raise ValueError('observed order must cover compute ops exactly')
    remaining = defaultdict(int)
    for t, readers in views[1].items():
        for o in readers: remaining[t, assignment[o]] += 1
    alive = set(); live = defaultdict(int); peak = defaultdict(int)
    degree = {o: len(ir.predecessors[o]) for o in ir.compute_ids}
    ready = [(rank[o], o) for o in ir.compute_ids if not degree[o]]; heapq.heapify(ready)
    order = []

    def effects(o):
        c = assignment[o]; allocated = defaultdict(int); released = defaultdict(int)
        for t in views[2][o]:
            pos = ir.tensors[t]['pos']; pos = 'UB' if pos == 'DDR' else pos
            size = ir.tensors[t]['size']; k = t, c
            if k not in alive: allocated[pos] += size
            count = remaining[k] - int(o in views[1][t])
            if count == 0: released[pos] += size
        return allocated, released

    while ready:
        choices = [heapq.heappop(ready) for _ in range(min(window, len(ready)))]
        base = choices[0][0]
        def priority(item):
            r, o = item; c = assignment[o]; alloc, release = effects(o)
            # Penalize temporary capacity excess as well as retained live bytes.
            delta = sum((alloc[p] - release[p] +
                         2 * max(0, live[c, p] + alloc[p] - capacity[p])) / capacity[p]
                        for p in capacity)
            return (weight * delta + .05 * (r - base) / window, r, o)
        chosen = min(choices, key=priority); o = chosen[1]; c = assignment[o]
        for item in choices:
            if item != chosen: heapq.heappush(ready, item)
        alloc, release = effects(o)
        for p in capacity:
            peak[c, p] = max(peak[c, p], live[c, p] + alloc[p])
            live[c, p] += alloc[p] - release[p]
            if live[c, p] < 0: raise AssertionError('negative surrogate live bytes')
        for t in views[2][o]:
            k = t, c
            if o in views[1][t]: remaining[k] -= 1
            if remaining[k]: alive.add(k)
            else: alive.discard(k)
        order.append(o)
        for nxt in ir.successors[o]:
            degree[nxt] -= 1
            if not degree[nxt]: heapq.heappush(ready, (rank[nxt], nxt))
    if len(order) != len(ir.compute_ids): raise ValueError('cyclic compute graph')
    return order, {f'{c}:{p}': value for (c, p), value in sorted(peak.items())}


def reuse_proposals(ir, assignment, timeline, cores, round_index=0):
    views = _tensor_views(ir); outputs = output_tensors(ir, views)
    weights = affinity(ir, views); loads = [defaultdict(int) for _ in range(cores)]
    for o in ir.compute_ids: loads[assignment[o]][ir.ops[o]['pipe']] += max(1, ir.ops[o]['cycles'])
    peak = max((max(x.values(), default=0) for x in loads), default=0)
    roots = [t for t in views[1] if not views[0][t] and len({assignment[o] for o in views[1][t]}) > 1]
    roots.sort(key=lambda t: (-ir.tensors[t]['size'] * (len({assignment[o] for o in views[1][t]}) - 1), t))
    seen = set(); proposals = []
    def add(t, source, dest, members, family):
        changes = {o: dest for o in members if assignment[o] == source}
        sig = tuple(sorted(changes.items()))
        if not changes or len(changes) > 2048 or sig in seen: return
        seen.add(sig); after = dict(assignment); after.update(changes)
        touched = set().union(*(views[2][o] for o in changes))
        delta = sum(tensor_copy_bytes(ir, q, after, views, outputs) -
                    tensor_copy_bytes(ir, q, assignment, views, outputs) for q in touched)
        loads_after = [dict(x) for x in loads]
        for o in changes:
            p = ir.ops[o]['pipe']; cost = max(1, ir.ops[o]['cycles'])
            loads_after[source][p] -= cost
            loads_after[dest][p] = loads_after[dest].get(p, 0) + cost
        peak_after = max(max(x.values(), default=0) for x in loads_after)
        proposals.append(dict(family=family, tensor_id=t, changes=changes,
            moved_ops=len(changes), partition_copy_delta=delta, static_peak_delta=peak_after-peak,
            proxy_delta=peak_after-peak+delta/60))
    for t in roots[:16]:
        grouped = defaultdict(list)
        for o in sorted(views[1][t]): grouped[assignment[o]].append(o)
        for source, readers in sorted(grouped.items()):
            destinations = sorted((c for c in grouped if c != source),
                                  key=lambda c: (max(loads[c].values(), default=0), c))[:2]
            for dest in destinations:
                add(t, source, dest, readers, 'input_readers')
                members = set(readers)
                for o in readers:
                    members.update(grow(ir, o, assignment, weights, 4 if round_index == 0 else 16, max(1,peak/4)))
                    if len(members) > 2048: break
                add(t, source, dest, members, 'input_regions')
                component_ids = {ir.component_by_op[o] for o in readers}
                if sum(len(ir.components[i].nodes) for i in component_ids) <= 2048:
                    add(t, source, dest, {o for i in component_ids for o in ir.components[i].nodes}, 'input_components')
    groups = defaultdict(list)
    for p in proposals: groups[p['family']].append(p)
    for group in groups.values(): group.sort(key=lambda x: (x['proxy_delta'], x['moved_ops'], x['tensor_id']))
    return [g[i] for i in range(max(map(len,groups.values()),default=0)) for g in groups.values() if i<len(g)]


def generate(ir, plan, raw, cores, limit=24, round_index=0):
    assignment = _plan_assignment(ir, plan, cores); views = _tensor_views(ir)
    timeline, _, diag = _trace(ir, assignment, raw, cores, views, plan)
    expected_bytes = partition_copy_bytes(ir, assignment, views)
    official_bytes = raw['data_movement_bytes']['scheduled_copy_bytes'] - raw['data_movement_bytes']['spill_added_copy_bytes']
    if expected_bytes != official_bytes: raise ValueError('partition COPY model disagrees with official result')
    order = _topology(ir, {o:(timeline[o]['start'], timeline[o]['end'], o) for o in ir.compute_ids})
    pools = [[], []]; seen = {json.dumps(plan, separators=(',', ':'))}
    def emit(pool, name, new_order, owners, metadata):
        value = _runs_plan(ir, new_order, owners, cores, max_run_ops=1)
        sig = json.dumps(value, separators=(',', ':'))
        if sig in seen: return
        seen.add(sig); validate_plan(ir, value)
        pool.append(dict(name=name, plan=value, metadata=metadata))
    control = []
    emit(control, 'data_encoding_control', order, assignment, dict(family='control',moved_ops=0))
    for window in ((8,32,128) if round_index == 0 else (16,64,256)):
        for weight in (.5, 2.):
            new_order, peak = lifetime_order(ir,assignment,order,window,weight,raw['capacity_bytes'])
            emit(pools[0],f'lifetime_w{window}_b{weight}',new_order,assignment,
                 dict(family='lifetime',window=window,weight=weight,moved_ops=0,
                      surrogate_peak=peak,partition_copy_delta=0,changed_positions=sum(a!=b for a,b in zip(order,new_order))))
    proposals = reuse_proposals(ir,assignment,timeline,cores,round_index)
    for i, proposal in enumerate(proposals[:limit]):
        after = dict(assignment); after.update(proposal['changes'])
        meta = {k:v for k,v in proposal.items() if k!='changes'}
        meta['changes'] = [dict(op=o,from_core=assignment[o],to_core=c) for o,c in sorted(proposal['changes'].items())]
        emit(pools[1], f'reuse_{round_index}_{i}', order, after, meta)
    candidates = (control + [g[i] for i in range(max(map(len,pools),default=0)) for g in pools if i<len(g)])[:limit]
    diag.update(candidate_count=len(candidates),reuse_proposals=len(proposals),partition_copy_bytes=expected_bytes,
                limitations='pre-spill bytes exact; ranking ignores overlap/cache; lifetime peaks are not official memory peaks')
    return candidates, diag
