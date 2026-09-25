"""Incumbent-local P1 split, regional reassignment and coalescing.

All intermediate transformations are free of official evaluations. Only the
complete candidate receives a necessary bound. Moves follow a topological order
of the incumbent's data AND core-order DAG, preserving untouched core sequences.
Ranking estimates future locality and load balance; it is not a pruning proof.
"""
from collections import Counter, defaultdict
import heapq
import json

from common_run import validate_plan
from p1_task_refine import view, split_large, coalesce
from p1_selective import task_lower_bound
from p1_boundary_lower_bound import boundary_ddr_lower_bound


def exact_key(plan):
    # Match evaluator semantics: retain object insertion and sequence ordering.
    return json.dumps(plan, ensure_ascii=False, separators=(',', ':'))


def task_order(ir, plan):
    """One extension of the existing data/core DAG; no implicit reordering."""
    _, owner, _, edges, pred = view(ir, plan, True)
    degree = {s: len(pred[s]) for s in owner}
    ready = [s for s in owner if degree[s] == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        s = heapq.heappop(ready)
        order.append(s)
        for child in sorted(edges[s]):
            degree[child] -= 1
            if degree[child] == 0:
                heapq.heappush(ready, child)
    if len(order) != len(owner):
        raise ValueError('cyclic incumbent')
    return order


def relocate(ir, plan, tasks, target, order=None):
    """Move complete current regions without changing operations or partition."""
    _, owner, _, _, _ = view(ir, plan)
    tasks = set(tasks)
    if not tasks or not tasks <= owner.keys():
        raise ValueError('nonempty known task set required')
    if type(target) is not int or not 0 <= target < len(plan['core_schedules']):
        raise ValueError('invalid target core')
    order = task_order(ir, plan) if order is None else order
    seq = [[] for _ in plan['core_schedules']]
    for s in order:
        seq[target if s in tasks else owner[s]].append(s)
    result = {'node_to_subgraph': dict(plan['node_to_subgraph']),
              'core_schedules': seq}
    validate_plan(ir, result)
    return result


def _move_specs(ir, plan):
    """Cheap diverse proposals, before complete-candidate construction."""
    mapping, owner, nodes, edges, pred = view(ir, plan)
    cores = len(plan['core_schedules'])
    work = {s: defaultdict(int) for s in owner}
    loads = [defaultdict(int) for _ in range(cores)]
    for op, sg in mapping.items():
        pipe, cost = ir.ops[op]['pipe'], max(1, ir.ops[op]['cycles'])
        work[sg][pipe] += cost
        loads[owner[sg]][pipe] += cost
    pipes = sorted({ir.ops[o]['pipe'] for o in ir.compute_ids})
    # Affinity only. Official P1 traffic is Task-boundary based, including same
    # core boundaries. This predicts possible later coalescing, not actual IO.
    producers, consumers = defaultdict(set), defaultdict(set)
    for e in ir.graph['edges']:
        if e['source'] in mapping:
            producers[e['target']].add(mapping[e['source']])
        if e['target'] in mapping:
            consumers[e['source']].add(mapping[e['target']])
    affinity = {s: defaultdict(int) for s in owner}
    for tensor, sources in producers.items():
        for a in sources:
            for b in consumers[tensor]:
                if a != b:
                    affinity[a][b] += ir.tensors[tensor]['size']
                    affinity[b][a] += ir.tensors[tensor]['size']
    # Restrict expensive neighbourhood growth to a deterministic seed panel.
    certificate = task_lower_bound(ir, plan)
    critical = [p['task'] for p in certificate['critical_task_path']]
    seeds = []
    if critical:
        seeds += [critical[i * (len(critical)-1)//3] for i in range(4)]
    seeds += sorted(owner, key=lambda s: (-max(work[s].values(), default=0), s))[:4]
    for core in sorted(range(cores), key=lambda c: -max(loads[c].values(), default=0)):
        seq = plan['core_schedules'][core]
        if seq:
            seeds += [seq[len(seq)//2]]
    seeds = list(dict.fromkeys(seeds))
    positions = {s: i for seq in plan['core_schedules'] for i, s in enumerate(seq)}
    groups = defaultdict(list)
    seen = set()
    for width in (1, 4, 16, 64):
        for seed in seeds:
            source = owner[seed]
            region = {seed}
            while len(region) < min(width, len(plan['core_schedules'][source])):
                adjacent = {n for s in region for n in edges[s] | pred[s]
                            if owner[n] == source and n not in region}
                if adjacent:
                    nxt = min(adjacent, key=lambda s: (
                        -sum(affinity[s].get(x, 0) for x in region),
                        abs(positions[s]-positions[seed]), s))
                else:
                    remaining = [s for s in plan['core_schedules'][source] if s not in region]
                    nxt = min(remaining, key=lambda s: (abs(positions[s]-positions[seed]), s))
                region.add(nxt)
            region_key = tuple(sorted(region))
            if len(region) == len(owner):
                # Moving the entire job to an otherwise empty core creates no
                # parallel work and is not the neighbourhood being studied.
                continue
            if (region_key, source) in seen:
                continue
            seen.add((region_key, source))
            regional_work = {p: sum(work[s][p] for s in region) for p in pipes}
            boundary = defaultdict(int)
            neighbours = set()
            for s in region:
                for n, weight in affinity[s].items():
                    if n not in region:
                        boundary[owner[n]] += weight
                neighbours.update((edges[s] | pred[s]) - region)
            old_remote = sum(owner[n] != source for n in neighbours)
            for target in range(cores):
                if target == source:
                    continue
                balance = max((loads[c][p] +
                               (regional_work[p] if c == target else
                                -regional_work[p] if c == source else 0))
                              for c in range(cores) for p in pipes)
                locality_delta = boundary[source] - boundary[target]
                wait_delta = sum(owner[n] != target for n in neighbours) - old_remote
                for style, value in (
                    ('balance', balance),
                    ('locality', balance + locality_delta/60 + 1000*wait_delta),
                ):
                    groups[width, style].append(dict(
                        tasks=region_key, source=source, target=target,
                        width=width, style=style, rank=value, balance_proxy=balance,
                        locality_delta_bytes=locality_delta, wait_edge_delta=wait_delta))
    for values in groups.values():
        values.sort(key=lambda m: (m['rank'], m['tasks'], m['target']))
    # One representative of each scale/style before any second representative.
    chosen, signatures = [], set()
    for offset in range(2):
        for values in groups.values():
            remaining = [m for m in values if (m['tasks'], m['target']) not in signatures]
            if not remaining:
                continue
            move = remaining[0]
            signatures.add((move['tasks'], move['target']))
            chosen.append(move)
    return chosen


def generate(ir, incumbent, max_moves=8, seed=17, combine=True):
    """Return complete candidates plus diagnostics. No evaluations or ID rules."""
    if type(max_moves) is not int or max_moves < 1:
        raise ValueError('positive move limit required')
    validate_plan(ir, incumbent)
    if len(incumbent['core_schedules']) <= 1:
        return [], {'reason': 'single_core', 'generated': 0}
    parents = [('incumbent', incumbent)]
    sizes = Counter(incumbent['node_to_subgraph'].values())
    if max(sizes.values(), default=0) > 512:
        parents.append(('split512', split_large(ir, incumbent, 512)))
    specs = [(name, parent, _move_specs(ir, parent)) for name, parent in parents]
    moves = []
    for i in range(max((len(s[2]) for s in specs), default=0)):
        for name, parent, proposals in specs:
            if i < len(proposals) and len(moves) < max_moves:
                moves.append((name, parent, proposals[i]))
    result, seen = [], {exact_key(incumbent)}
    old_owner = {s: c for c, seq in enumerate(incumbent['core_schedules']) for s in seq}
    old_assignment = {o: old_owner[s] for o, s in incumbent['node_to_subgraph'].items()}
    orders = {name: task_order(ir, parent) for name, parent, _ in specs}
    for index, (parent_name, parent, move) in enumerate(moves):
        moved = relocate(ir, parent, move['tasks'], move['target'], orders[parent_name])
        counts = sorted(Counter(moved['node_to_subgraph'].values()).values())
        # Preserve the teammate's successful fine-task and normal-task scales.
        cap = 8 if counts[len(counts)//2] <= 8 else 256
        final = coalesce(ir, moved, cap, seed=seed) if combine else moved
        signature = exact_key(final)
        if signature in seen:
            continue
        seen.add(signature)
        validate_plan(ir, final)
        owner = {s: c for c, seq in enumerate(final['core_schedules']) for s in seq}
        changed = [int(o) for o, s in final['node_to_subgraph'].items()
                   if owner[s] != old_assignment[o]]
        if not changed:
            continue
        tb = task_lower_bound(ir, final)['value']
        db = boundary_ddr_lower_bound(ir, final)['lower_bound']
        result.append(dict(
            name=f'region_{index}_{parent_name}_w{move["width"]}_{move["style"]}'
                 + (f'_merge{cap}' if combine else '_unmerged'),
            plan=final,
            metadata=dict(parent=parent_name, **move, moved_ops=len(changed),
                          merge_cap=cap if combine else None, task_bound=tb,
                          ddr_bound=db, lower_bound=max(tb, db),
                          tasks_after=len(owner),
                          proxy_scope='heuristic load/locality only; not an IO or time certificate')))
    return result, dict(generated=len(result), proposed_moves=len(moves),
                        parents=[name for name, _, _ in specs], combine=combine,
                        scope='P1 incumbent-local region move; bound evaluated after complete composition')
