"""Bounded cross-old-Task P1 repartitioning; no case identifiers or evaluator calls.

Build complete plans before scoring. Outside Tasks retain ownership and relative
order. Proxies rank candidates only: DDR contention and spills require evaluation.
"""
from collections import defaultdict
import heapq
import time
from common_run import validate_plan
from p1_task_refine import view
from p1_selective import topological_order, _toposort_blocks
from partition_candidates import block_views


def tensor_affinity(ir):
    compute = set(ir.compute_ids)
    producers, consumers = defaultdict(list), defaultdict(list)
    for edge in ir.graph['edges']:
        a, b = edge['source'], edge['target']
        if a in ir.ops:
            producers[b].append(a)
        else:
            consumers[a].append(b)
    affinity = defaultdict(int)
    for tid, tensor in ir.tensors.items():
        for a in producers[tid]:
            for b in consumers[tid]:
                if a in compute and b in compute:
                    affinity[a, b] += tensor['size']
    return affinity


def partition_region(ir, members, target_work, family, order, affinity, deadline=float('inf')):
    """A ready frontier guarantees an acyclic regional quotient.

    branch: end a chain at forks/joins, avoiding whole-branch barriers.
    affinity: grow a connected ready frontier through the largest live edges.
    Both can combine operations from different old Tasks; global legality is
    separately checked because paths may leave and re-enter the region.
    """
    members = set(members)
    pred = {o: set(ir.predecessors[o]) & members for o in members}
    succ = {o: set(ir.successors[o]) & members for o in members}
    degree = {o: len(pred[o]) for o in members}
    tail = {}
    for o in reversed(order):
        if o in members:
            tail[o] = ir.ops[o]['cycles'] + max((tail[c] for c in succ[o]), default=0)
    ready = {o for o in members if not degree[o]}
    blocks = []
    while ready:
        if time.perf_counter() > deadline:
            raise TimeoutError('candidate construction deadline')
        root = min(ready, key=lambda o: (-tail[o], o))
        block, inside, work = [], set(), [0, 0]
        options = {root}
        while options:
            def priority(o):
                shared = sum(affinity[p, o] for p in pred[o] if p in inside)
                return (-shared, -tail[o], o) if family == 'affinity' else (-tail[o], o)
            op = min(options, key=priority)
            pipe = 0 if ir.ops[op]['pipe'] == 'PIPE_M' else 1
            if block and work[pipe] + ir.ops[op]['cycles'] > target_work:
                options.remove(op)
                continue
            ready.remove(op); block.append(op); inside.add(op)
            work[pipe] += ir.ops[op]['cycles']
            for child in succ[op]:
                degree[child] -= 1
                if not degree[child]:
                    ready.add(child)
            if family == 'branch':
                # Leave fork outputs available together, rather than burying
                # their producer inside one long branch's Task.
                options = {c for c in succ[op] if c in ready and len(pred[c]) == 1} if len(succ[op]) == 1 else set()
            else:
                options = {c for p in inside for c in succ[p] if c in ready}
        blocks.append(block)
    if sum(map(len, blocks)) != len(members):
        raise ValueError('regional compute cycle')
    return blocks


def prepare(ir, incumbent, region, replacement, order):
    _, owner, nodes, _, _ = view(ir, incumbent)
    outside = {t for t in owner if t not in region}
    blocks = [nodes[t] for t in sorted(outside)] + replacement
    blocks = _toposort_blocks(ir, blocks, order)
    bv = block_views(ir, blocks)
    fixed = {bv['mapping'][nodes[t][0]]: owner[t] for t in outside}
    preds = [set(p) for p in bv['preds']]
    for seq in incumbent['core_schedules']:
        old = [bv['mapping'][nodes[t][0]] for t in seq if t in outside]
        for a, b in zip(old, old[1:]):
            preds[b].add(a)
    return blocks, bv, fixed, preds


def schedule(ir, incumbent, blocks, bv, fixed, preds, width=1, deadline=float('inf')):
    """Critical-tail list order with width-4 joint core-allocation beam.

    Width 1 reproduces an analytical EFT heuristic with outside constraints;
    width 4 retains competing assignments instead of committing immediately.
    Task durations charge all inter-Task DDR, including same-core boundaries.
    No measured duration from an obsolete partition is reused.
    """
    count, ncores = len(blocks), len(incumbent['core_schedules'])
    succ = [set() for _ in blocks]
    for b, ps in enumerate(preds):
        for a in ps:
            succ[a].add(b)
    degree = list(map(len, preds)); q = [i for i in range(count) if not degree[i]]; heapq.heapify(q)
    topo = []
    while q:
        a = heapq.heappop(q); topo.append(a)
        for b in sorted(succ[a]):
            degree[b] -= 1
            if not degree[b]: heapq.heappush(q, b)
    if len(topo) != count:
        raise ValueError('region contraction conflicts with outside core order')
    durations = [bv['work'][b][2] + (sum(ir.input_sizes[t] for t in bv['root_inputs'][b])
                 + 2 * bv['boundary_bytes'][b]) / 60 for b in range(count)]
    tail = {}
    for a in reversed(topo):
        tail[a] = durations[a] + max((tail[b] for b in succ[a]), default=0)
    degree = list(map(len, preds)); q = [(-tail[b], b) for b in range(count) if not degree[b]]; heapq.heapify(q)
    priority_order = []
    while q:
        _, a = heapq.heappop(q); priority_order.append(a)
        for b in sorted(succ[a]):
            degree[b] -= 1
            if not degree[b]: heapq.heappush(q, (-tail[b], b))
    # owner, end, core availability, scheduled counts. Same topological order
    # means outside relative order survives every beam state.
    states = [([-1] * count, [0.] * count, [0.] * ncores, [0] * ncores)]
    for a in priority_order:
        if time.perf_counter() > deadline:
            raise TimeoutError('candidate construction deadline')
        next_states = []
        for assignment, ends, available, lengths in states:
            for core in ([fixed[a]] if a in fixed else range(ncores)):
                release = max((ends[p] + (1000 if assignment[p] != core else 0) for p in preds[a]), default=0)
                end = max(release, available[core] + (100 if lengths[core] else 0)) + durations[a]
                av = available.copy(); av[core] = end
                if width == 1:
                    rank = (end, lengths[core], core)
                else:
                    rank = (max(max(av), end + tail[a] - durations[a]), max(av), sum(av), core)
                next_states.append((rank, assignment, ends, av, lengths, core, end))
        states = []
        for _, assignment, ends, av, lengths, core, end in sorted(next_states, key=lambda s: s[0])[:width]:
            assignment = assignment.copy(); ends = ends.copy(); lengths = lengths.copy()
            assignment[a] = core; ends[a] = end; lengths[core] += 1
            states.append((assignment, ends, av, lengths))
    assignment, _, available, _ = min(states, key=lambda s: (max(s[2]), sum(s[2])))
    schedules = [[] for _ in range(ncores)]
    for a in priority_order:
        schedules[assignment[a]].append(a)
    # Preserve original mapping insertion order, which the official builder uses.
    result = {'node_to_subgraph': {o: bv['mapping'][int(o)] for o in incumbent['node_to_subgraph']},
              'core_schedules': schedules}
    validate_plan(ir, result)
    return result, max(available)


def merge_region(ir, plan, protected_ops, target_work, deadline=float('inf')):
    """Contract consecutive regional same-core Tasks, validating every contraction."""
    result = plan
    for seq in plan['core_schedules']:
        for first, second in zip(seq, seq[1:]):
            if time.perf_counter() > deadline:
                return result
            mp, _, nodes, _, _ = view(ir, result)
            if first not in nodes or second not in nodes:
                continue
            members = nodes[first] + nodes[second]
            if any(o in protected_ops for o in members):
                continue
            work = max(sum(ir.ops[o]['cycles'] for o in members if ir.ops[o]['pipe'] == pipe)
                       for pipe in ('PIPE_M', 'PIPE_V'))
            if work > target_work:
                continue
            proposal = {'node_to_subgraph': {o: first if s == second else s for o, s in result['node_to_subgraph'].items()},
                        'core_schedules': [[s for s in cs if s != second] for cs in result['core_schedules']]}
            try:
                validate_plan(ir, proposal)
            except ValueError:
                continue
            result = proposal
    return result


def generate(ir, incumbent, selected_regions, seconds=30):
    started = time.perf_counter(); deadline = started + seconds
    order = topological_order(ir, 'stable_id'); affinity = tensor_affinity(ir)
    mp, _, nodes, _, _ = view(ir, incumbent)
    candidates, rejected = [], []
    for index, region in enumerate(selected_regions):
        members = {o for t in region for o in nodes[t]}
        total = max(sum(ir.ops[o]['cycles'] for o in members if ir.ops[o]['pipe'] == pipe)
                    for pipe in ('PIPE_M', 'PIPE_V'))
        for scale in (1, 2, 4):
            target = max(1, total / (len(incumbent['core_schedules']) * scale))
            for family in ('branch', 'affinity'):
                name = f'r{index}_{family}_s{scale}'
                try:
                    if time.perf_counter() > deadline: raise TimeoutError('candidate construction deadline')
                    replacement = partition_region(ir, members, target, family, order, affinity, deadline)
                    blocks, bv, fixed, preds = prepare(ir, incumbent, region, replacement, order)
                    for width in (1, 4):
                        plan, proxy = schedule(ir, incumbent, blocks, bv, fixed, preds, width, deadline)
                        mixed = sum(len({mp[o] for o in block}) > 1 for block in replacement)
                        candidates.append({'name': name + f'_w{width}', 'plan': plan, 'metadata': {
                            'region': index, 'old_tasks': region, 'region_ops': len(members), 'family': family,
                            'scale': scale, 'width': width, 'target_work': target, 'proxy': proxy,
                            'tasks': len(blocks), 'cross_old_boundary_blocks': mixed}})
                except (ValueError, TimeoutError) as error:
                    rejected.append({'name': name, 'error': str(error)})
    return candidates, {'seconds': time.perf_counter() - started, 'rejected': rejected,
                        'proxy_scope': 'Ranking only. Shared DDR contention, cache lifetimes and spills omitted.'}
