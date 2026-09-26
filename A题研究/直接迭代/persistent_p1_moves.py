"""Incremental, trace-directed operation-region repairs for P1.

No historical solution or official evaluator is used here. A selected operation
window can cut a giant old Task and cross several old Task boundaries. Regional
partition, allocation and order are changed together. Unselected original Tasks
keep their ownership and relative core order unless boundary relaxation is
explicitly requested. Proxy scores are descriptive, never an acceptance test.
"""
from collections import Counter, defaultdict
import heapq
import time

from common_run import validate_plan
from p1_task_refine import view
from p1_selective import topological_order, _toposort_blocks
from partition_candidates import block_views
from p1_bottleneck_repartition import partition_region, tensor_affinity, schedule


def _check(deadline):
    if time.perf_counter() >= deadline:
        raise TimeoutError('P1 regional construction deadline')


def _trace(ir, plan, raw, observation):
    """Use the cached compiler observation; never silently compile it again."""
    mp, owner, nodes, edges, preds = view(ir, plan)
    tasks = {t['task_id']: t for c in raw.get('per_core_timeline', [])
             for t in c.get('tasks', [])}
    events = {o['op_id']: o for c in raw.get('per_core_timeline', [])
              for o in c.get('ops', []) if o['op_id'] in mp}
    chain_weight = Counter()
    chain_kind = Counter()
    if observation:
        for key in observation.get('chain', []):
            e = observation['events'][key]
            chain_weight[key[0]] += max(1, e['duration'])
        chain = observation.get('chain', [])
        for a, b in zip(chain[1:], chain):
            edge = observation.get('pred', {}).get(b, {}).get(a)
            if edge:
                chain_kind[edge[1]] += 1
        source = 'cached_official_compiled_chain'
    else:
        # Exact Task releases, but not a claim to observe internal memory/DDR
        # causality. This fallback is useful when observation itself times out.
        previous = {b: a for seq in plan['core_schedules'] for a, b in zip(seq, seq[1:])}
        cur = max(tasks, key=lambda t: (tasks[t]['end'], t), default=None)
        visited = set()
        while cur is not None and cur not in visited:
            visited.add(cur)
            chain_weight[cur] += max(1, tasks[cur]['duration'])
            opts = [(tasks[p]['end'] + (raw.get('task_cross_core_wait_cycles', 1000)
                      if owner[p] != owner[cur] else 0), p, 'task_data')
                    for p in preds[cur] if p in tasks]
            if cur in previous and previous[cur] in tasks:
                p = previous[cur]
                opts.append((tasks[p]['end'] + raw.get('task_same_core_wait_cycles', 100), p, 'task_core'))
            _, cur, kind = max(opts, default=(0, None, 'source'))
            chain_kind[kind] += 1
        source = 'task_release_fallback' if tasks else 'static_compute_fallback'
    work = {t: max(sum(max(1, ir.ops[o]['cycles']) for o in ns if ir.ops[o]['pipe'] == pipe)
                   for pipe in ('PIPE_M', 'PIPE_V')) for t, ns in nodes.items()}
    hot = sorted(nodes, key=lambda t: (-chain_weight[t], -tasks.get(t, {}).get('duration', work[t]), t))
    return dict(mapping=mp, owner=owner, nodes=nodes, edges=edges, preds=preds,
                events=events, tasks=tasks, hot=hot, chain_weight=chain_weight,
                chain_kind=dict(chain_kind), source=source)


def select_region(ir, trace, cap, round_index, deadline):
    """Bounded connected frontier, filled with time-adjacent independent work.

    Crossing an old Task boundary receives priority, but never forces an entire
    neighboring Task into the window. This handles one huge Task as naturally as
    many small Tasks. Round index rotates the hot Task/window after a failed move.
    """
    mp, nodes, events = trace['mapping'], trace['nodes'], trace['events']
    hot = trace['hot']
    if not hot:
        return set(), {}
    seed_task = hot[(round_index // 2) % min(3, len(hot))]
    ordered = sorted(nodes[seed_task], key=lambda o: (events.get(o, {}).get('start', o), o))
    # Overlapping temporal windows expose distinct parts of a giant Task.
    anchor = ordered[min(len(ordered)-1, (round_index % 3) * len(ordered) // 3)]
    anchor_time = events.get(anchor, {}).get('start', anchor)
    ranks = {t: i for i, t in enumerate(hot)}
    selected, queued, frontier = set(), set(), []
    def add(op, distance=0):
        if op in queued:
            return
        queued.add(op)
        crossing = mp[op] != seed_task
        key = (distance, 0 if crossing else 1,
               abs(events.get(op, {}).get('start', op)-anchor_time),
               ranks.get(mp[op], len(hot)), op)
        heapq.heappush(frontier, (key, op))
    add(anchor)
    # Seed an actual data boundary even when the temporal anchor is deep inside
    # a long branch; this prevents a nominal regional move confined to one Task.
    boundary = [o for o in ordered if any(mp[p] != seed_task
                for p in ir.predecessors[o]+ir.successors[o])]
    if boundary:
        b = min(boundary, key=lambda o: (abs(events.get(o, {}).get('start', o)-anchor_time), o))
        add(b)
        for p in ir.predecessors[b]+ir.successors[b]:
            if mp[p] != seed_task:
                add(p)
    fill = iter(sorted(ordered, key=lambda o: (abs(events.get(o, {}).get('start', o)-anchor_time), o)))
    while len(selected) < min(cap, len(ir.compute_ids)):
        _check(deadline)
        if not frontier:
            for op in fill:
                if op not in selected:
                    add(op)
                    break
            if not frontier:
                break
        (key, op) = heapq.heappop(frontier)
        if op in selected:
            continue
        selected.add(op)
        for p in ir.predecessors[op]+ir.successors[op]:
            add(p, key[0]+1)
    touched = sorted({mp[o] for o in selected})
    return selected, dict(seed_task=seed_task, anchor_op=anchor,
                         old_tasks=touched, region_ops=len(selected), cap=cap,
                         partial_old_tasks=[t for t in touched if not set(nodes[t]) <= selected])


def _acyclic_pieces(ir, blocks, order, deadline):
    """Repair contraction cycles by *splitting*, never re-fusing the bottleneck.

    On a quotient cycle, refine only groups in a cyclic SCC. For each operation,
    its boundary level is max(pred level + [old group changes]). An edge inside
    an equal-level group stays internal; any remaining quotient edge increases
    level, so the refined SCC quotient is acyclic. The SCC condensation remains
    a DAG and all noncyclic groups remain untouched.
    """
    try:
        return _toposort_blocks(ir, blocks, order), set()
    except ValueError as error:
        if 'quotient cycle' not in str(error):
            raise
    mp = {op: b for b, ns in enumerate(blocks) for op in ns}
    succ = [set() for _ in blocks]
    reverse = [set() for _ in blocks]
    for op in order:
        for nxt in ir.successors[op]:
            a, b = mp[op], mp[nxt]
            if a != b and b not in succ[a]:
                succ[a].add(b); reverse[b].add(a)
    seen, finish = set(), []
    for root in range(len(blocks)):
        if root in seen: continue
        stack = [(root, False)]
        while stack:
            _check(deadline)
            a, done = stack.pop()
            if done:
                finish.append(a); continue
            if a in seen: continue
            seen.add(a); stack.append((a, True))
            stack.extend((b, False) for b in sorted(succ[a], reverse=True) if b not in seen)
    scc, seen, component = {}, set(), 0
    for root in reversed(finish):
        if root in seen: continue
        group, stack = [], [root]; seen.add(root)
        while stack:
            a = stack.pop(); group.append(a)
            for b in reverse[a]:
                if b not in seen: seen.add(b); stack.append(b)
        if len(group) > 1:
            for b in group: scc[b] = component
            component += 1
    unresolved = set(scc)
    level, groups = {}, defaultdict(list)
    for op in order:
        _check(deadline)
        if mp[op] not in unresolved:
            continue
        level[op] = max((level[p] + int(mp[p] != mp[op])
                        for p in ir.predecessors[op] if mp[p] in unresolved
                        and scc[mp[p]] == scc[mp[op]]), default=0)
        groups[mp[op], level[op]].append(op)
    refined = [block for b, block in enumerate(blocks) if b not in unresolved] + list(groups.values())
    counts = Counter(b for b, _ in groups)
    cut_ops = {o for b, ns in enumerate(blocks) if counts[b] > 1 for o in ns}
    return _toposort_blocks(ir, refined, order), cut_ops


def prepare_region(ir, old, members, replacement, order, deadline, relax=False):
    """Keep residual memberships/owners; repair only actual quotient conflicts."""
    mp, owner, nodes, edges, old_preds = view(ir, old)
    touched = {mp[o] for o in members}
    residual = [list(o for o in ns if o not in members) for ns in nodes.values()]
    blocks, repair_ops = _acyclic_pieces(ir, [b for b in residual if b] + replacement, order, deadline)
    bv = block_views(ir, blocks)
    fixed = {b: owner[mp[ns[0]]] for b, ns in enumerate(blocks) if not set(ns) & members}
    preds = [set(ps) for ps in bv['preds']]
    unmodified = set(nodes) - touched
    relaxed_tasks = set()
    if relax:
        # Explicit one-hop boundary-order release. Ownership is still protected.
        relaxed_tasks = {t for s in touched for t in edges[s] | old_preds[s]} - touched
    outside_blocks = defaultdict(list)
    for b, ns in enumerate(blocks):
        if mp[ns[0]] in unmodified and b in fixed:
            outside_blocks[mp[ns[0]]].append(b)
    for seq in old['core_schedules']:
        kept = [t for t in seq if t in unmodified and t not in relaxed_tasks]
        for a, b in zip(kept, kept[1:]):
            for pa in outside_blocks[a]:
                for pb in outside_blocks[b]: preds[pb].add(pa)
    return blocks, bv, fixed, preds, dict(
        boundary_repair_ops=len(repair_ops-members),
        boundary_repair_old_tasks=sorted({mp[o] for o in repair_ops-members}),
        outside_order_relaxed_tasks=sorted(relaxed_tasks),
        protected_whole_tasks=sorted(unmodified-relaxed_tasks-{mp[o] for o in repair_ops}))


def _merge_small(ir, plan, members, target, deadline, max_ops=32):
    """Contract regional intervals of a data+core topological order safely."""
    mp, owner, nodes, edges, preds = view(ir, plan, core_edges=True)
    eligible = {t for t, ns in nodes.items() if set(ns) <= members}
    task_work = {}
    for t, ns in nodes.items():
        work = Counter()
        for op in ns: work[ir.ops[op]['pipe']] += max(1, ir.ops[op]['cycles'])
        task_work[t] = work
    degree = {t: len(ps) for t, ps in preds.items()}
    ready = {t for t in nodes if not degree[t]}
    groups, group, size, work, core = [], [], 0, Counter(), None
    while ready:
        _check(deadline)
        choices = [t for t in ready if t in eligible and owner[t] == core
                   and size+len(nodes[t]) <= max_ops
                   and max((work[p]+task_work[t][p] for p in set(work)|set(task_work[t])), default=0) <= target]
        if not choices:
            if group: groups.append(group)
            group, size, work, core = [], 0, Counter(), None
        task = min(choices or ready); ready.remove(task)
        if task not in eligible:
            groups.append([task])
        else:
            group.append(task); size += len(nodes[task]); work.update(task_work[task]); core = owner[task]
        for b in edges[task]:
            degree[b] -= 1
            if not degree[b]: ready.add(b)
    if group: groups.append(group)
    if sum(map(len, groups)) != len(nodes): raise ValueError('cyclic candidate before merge')
    replace = {t: group[0] for group in groups for t in group[1:]}
    if not replace:
        return plan, 0
    result = dict(node_to_subgraph={o: replace.get(t, t) for o, t in plan['node_to_subgraph'].items()},
                  core_schedules=[[t for t in seq if t not in replace] for seq in plan['core_schedules']])
    validate_plan(ir, result)
    return result, len(replace)


def iter_candidates(ir, plan, raw, cores, round_index=0, observation=None, seconds=12):
    """Yield complete repairs incrementally, charging generation-only elapsed.

    Resuming after an official evaluation resets the segment timer. The external
    controller additionally imposes a hard timeout on next(); it should close a
    timed-out generator. Up to four candidates cover two bounded region widths
    and raw/small-Task-coalesced versions, without growing an unbounded queue.
    """
    if cores != len(plan['core_schedules']) or cores not in range(1, 6):
        raise ValueError('plan/core count mismatch')
    if seconds <= 0 or not ir.compute_ids:
        return
    start = time.perf_counter(); spent = 0.
    deadline = start + seconds
    validate_plan(ir, plan)
    trace = _trace(ir, plan, raw, observation)
    order = topological_order(ir, 'stable_id')
    affinity = tensor_affinity(ir)
    seen = set()
    oldmp, oldowner, oldnodes, _, _ = view(ir, plan)
    failures = []
    for width_index, cap in enumerate((4096, 8192)):
        try:
            _check(deadline)
            members, region_meta = select_region(ir, trace, cap, round_index, deadline)
            if not members: continue
            total = max(sum(max(1, ir.ops[o]['cycles']) for o in members if ir.ops[o]['pipe'] == pipe)
                        for pipe in ('PIPE_M', 'PIPE_V'))
            family = 'affinity' if round_index % 2 == 0 else 'branch'
            # A tiny region must not degenerate into one-cycle Tasks whose
            # mandatory Task release costs dominate the compute being exposed.
            target = max(1., 2*raw.get('task_cross_core_wait_cycles', 1000),
                         total/(cores*(32 if width_index == 0 else 16)))
            replacement = partition_region(ir, members, target, family, order, affinity, deadline)
            blocks, bv, fixed, preds, repair_meta = prepare_region(
                ir, plan, members, replacement, order, deadline, relax=bool(width_index))
            candidate, proxy = schedule(ir, plan, blocks, bv, fixed, preds, 1, deadline)
            for merge in (False, True):
                _check(deadline)
                result, merges = _merge_small(ir, candidate, members, target*2, deadline) if merge else (candidate, 0)
                signature = (tuple(result['node_to_subgraph'].items()), tuple(map(tuple, result['core_schedules'])))
                if signature in seen: continue
                seen.add(signature)
                newmp, newowner, newnodes, _, _ = view(ir, result)
                changed = [t for t, ns in oldnodes.items() if len({newmp[o] for o in ns}) > 1]
                mixed = sum(len({oldmp[o] for o in ns}) > 1 for ns in newnodes.values())
                moved = sum(oldowner[oldmp[o]] != newowner[newmp[o]] for o in ir.compute_ids)
                metadata = dict(region=region_meta, **repair_meta,
                    changed_old_boundaries=changed, cross_old_boundary_blocks=mixed,
                    moved_operations=moved, family=family, target_work=target,
                    merged_tasks=merges, tasks=len(newnodes), max_task_ops=max(map(len, newnodes.values())),
                    actual_actions=['operation_window', 'repartition', 'eft_allocation', 'task_order']
                                   + (['regional_coalesce'] if merges else []),
                    observation_source=trace['source'], observed_chain_edge_kinds=trace['chain_kind'],
                    proxy_end=proxy, generation_failures=list(failures),
                    limitations='Local static allocation proxy; official DDR/memory effects decide acceptance.')
                spent += time.perf_counter()-start
                metadata['generation_seconds_cumulative'] = spent
                yield dict(name=f'p1_region_r{round_index}_cap{cap}_{family}'+('_merge' if merge else '_raw'),
                           plan=result, metadata=metadata)
                # Time spent suspended in yield belongs to the caller, not this
                # module's cumulative generation budget.
                start = time.perf_counter(); deadline = start+max(0., seconds-spent)
        except TimeoutError:
            # Also preserves an outer SIGALRM timeout: the controller must
            # record it as generation timeout, never as queue exhaustion.
            raise
        except ValueError as exc:
            failures.append(dict(cap=cap, error=str(exc)))
            continue
    return dict(generation_failures=failures)
