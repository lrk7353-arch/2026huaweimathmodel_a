"""P1 common complete-plan proxy and ready-task/core joint search.

This analytical objective omits shared DDR arbitration and spill timing. All
final candidates require official evaluation; trace times only localize regions.
"""
from collections import Counter, defaultdict
import heapq
import math
import time

from common_run import validate_plan
from p1_task_refine import view
from p1_selective import topological_order
from partition_candidates import block_views


def _topo(preds):
    succ = [set() for _ in preds]
    degree = list(map(len, preds))
    for b, parents in enumerate(preds):
        for a in parents:
            if a == b:
                raise ValueError('self edge in block DAG')
            succ[a].add(b)
    ready = [a for a, n in enumerate(degree) if not n]
    heapq.heapify(ready)
    order = []
    while ready:
        a = heapq.heappop(ready); order.append(a)
        for b in sorted(succ[a]):
            degree[b] -= 1
            if not degree[b]: heapq.heappush(ready, b)
    if len(order) != len(preds):
        raise ValueError('partition plus fixed outside order is cyclic')
    return order, succ


def _raw_model(ir, incumbent, blocks, fixed, preferred_owner, metadata=None,
               extra_preds=None):
    """Normalize a complete partition and preserve all requested core edges."""
    flat = [op for block in blocks for op in block]
    if any(not block for block in blocks) or len(flat) != len(set(flat)) or set(flat) != set(ir.compute_ids):
        raise ValueError('blocks must cover every compute operation exactly once')
    op_block = {op: b for b, block in enumerate(blocks) for op in block}
    preds = [set() for _ in blocks]
    for op in ir.compute_ids:
        for child in ir.successors[op]:
            a, b = op_block[op], op_block[child]
            if a != b: preds[b].add(a)
    if extra_preds is not None:
        for b, parents in enumerate(extra_preds): preds[b].update(parents)
    topo, _ = _topo(preds)
    renumber = {old: new for new, old in enumerate(topo)}
    op_order = {op: i for i, op in enumerate(topological_order(ir, 'stable_id'))}
    normalized = [sorted(blocks[old], key=op_order.__getitem__) for old in topo]
    bv = block_views(ir, normalized)
    normalized_preds = [{renumber[p] for p in preds[old]} for old in topo]
    root = [sum(ir.input_sizes[t] for t in ts) for ts in bv['root_inputs']]
    durations = [float(bv['work'][b][2]) + (root[b] + 2 * bv['boundary_bytes'][b]) / 60.0
                 for b in range(len(normalized))]
    meta = dict(metadata or {})
    meta['input_block_to_model'] = renumber
    meta['proxy_scope'] = 'Static complete candidate; shared DDR arbitration, memory spill and detailed pipe overlap omitted.'
    return dict(blocks=normalized, preds=normalized_preds,
                fixed={renumber[b]: c for b, c in fixed.items()},
                active=[renumber[b] for b in topo if b not in fixed],
                durations=durations, ncores=len(incumbent['core_schedules']),
                incumbent=incumbent, preferred_owner=[preferred_owner[b] for b in topo],
                metadata=meta, root_bytes=[root[b] for b in range(len(normalized))],
                boundary_bytes=list(bv['boundary_bytes']), work=list(bv['work']),
                mapping=bv['mapping'])


def _localize(ir, incumbent, raw):
    """Rank observed tight producer/consumer gates, using latest required op."""
    mapping, owner, nodes, edges, pred = view(ir, incumbent)
    tasks = {e['task_id']: e for c in raw.get('per_core_timeline', []) for e in c.get('tasks', [])}
    events = {e['op_id']: e for c in raw.get('per_core_timeline', []) for e in c.get('ops', []) if e['op_id'] in mapping}
    if set(tasks) != set(owner) or set(events) != set(mapping):
        raise ValueError('region localization requires a matching complete official trace')
    if any(e.get('task_id', mapping[op]) != mapping[op] for op, e in events.items()):
        raise ValueError('trace compute ownership differs from incumbent')
    # Core records may be serialized in any order.
    timelines = {entry.get('core_id', c): entry for c, entry in enumerate(raw['per_core_timeline'])}
    if set(timelines) != set(range(len(incumbent['core_schedules']))):
        raise ValueError('trace core IDs differ from incumbent')
    # Trace ownership/order must correspond exactly to the current incumbent.
    for c, seq in enumerate(incumbent['core_schedules']):
        timeline = timelines[c]
        if [e['task_id'] for e in timeline['tasks']] != seq:
            raise ValueError('trace Task order differs from incumbent')
    previous = {b: a for seq in incumbent['core_schedules'] for a, b in zip(seq, seq[1:])}
    cross, same = raw.get('task_cross_core_wait_cycles', 1000), raw.get('task_same_core_wait_cycles', 100)
    core_ready = {t: tasks[previous[t]]['end'] + same if t in previous else 0 for t in owner}
    releases = {t: {p: tasks[p]['end'] + (cross if owner[p] != owner[t] else 0) for p in pred[t]} for t in owner}
    chain, pending = set(), [t for t in owner if tasks[t]['end'] == raw['makespan']]
    while pending:
        t = pending.pop()
        if t in chain: continue
        chain.add(t)
        pending.extend(p for p, release in releases[t].items() if release == tasks[t]['start'])
        if t in previous and core_ready[t] == tasks[t]['start']: pending.append(previous[t])
    witnesses = {}
    for target_op in ir.compute_ids:
        for source_op in ir.predecessors[target_op]:
            a, b = mapping[source_op], mapping[target_op]
            if a == b: continue
            witness = (events[source_op]['end'], source_op, target_op)
            if (a, b) not in witnesses or witness > witnesses[a, b]: witnesses[a, b] = witness
    ranked = []
    for (a, b), (op_end, source_op, target_op) in witnesses.items():
        release = releases[b][a]
        other = max((r for p, r in releases[b].items() if p != a), default=0)
        gate = max(core_ready[b], max(releases[b].values(), default=0))
        alternative = max(core_ready[b], other, op_end + (cross if owner[a] != owner[b] else 0))
        opportunity = max(0, gate - alternative)
        ranked.append((-(b in chain and opportunity > 0), -opportunity,
                       -(tasks[a]['end'] - op_end), a, b, source_op, target_op))
    ranked.sort()
    if ranked:
        _, neg_opportunity, _, a, b, source_op, target_op = ranked[0]
        primary = [a, b]
        witness = dict(source_task=a, target_task=b, source_op=source_op, target_op=target_op,
                       optimistic_localization_cycles=-neg_opportunity)
    else:
        primary = [max(owner, key=lambda t: (t in chain, tasks[t]['end'] - tasks[t]['start']))]
        witness = None
    # One-hop neighboring Tasks are released in ownership, not just in order.
    neighbors = set().union(*(pred[t] | edges[t] for t in primary)) - set(primary)
    neighbor_order = sorted(neighbors, key=lambda t: (t not in chain, -len(nodes[t]), t))
    return mapping, owner, nodes, primary, neighbor_order, witness, tasks


def build_problem(ir, incumbent, raw, scope='small', deadline=float('inf')):
    started = time.perf_counter()
    if scope not in ('small', 'large'): raise ValueError('unknown scope')
    validate_plan(ir, incumbent)
    mapping, owner, nodes, primary, neighbors, witness, tasks = _localize(ir, incumbent, raw)
    halo = neighbors[:2 if scope == 'small' else 4]
    selected = set(primary + halo)
    target_count = 20 if scope == 'small' else 40
    order = topological_order(ir, 'stable_id')
    by_task = defaultdict(list)
    for op in order: by_task[mapping[op]].append(op)
    # Allocate split count by normalized M/V work, retaining each original
    # Task's internal topological order. Actual count is recorded, never faked.
    selected_work = {t: max(1, sum(ir.ops[o]['cycles'] for o in nodes[t])) for t in selected}
    quotas = {t: 1 for t in selected}
    for _ in range(max(0, target_count - len(selected))):
        choices = [t for t in selected if quotas[t] < len(nodes[t])]
        if not choices: break
        t = max(choices, key=lambda t: (selected_work[t] / quotas[t], -t))
        quotas[t] += 1
    blocks, fixed, preferred, task_blocks = [], {}, [], defaultdict(list)
    for t in sorted(owner):
        ops = by_task[t]
        count = quotas.get(t, 1)
        # Place the observed critical producer's latest-needed computation
        # exactly at an atom boundary. Other cuts are deterministic quantiles.
        # Fine atoms define a common backend comparison, not final groups.
        cuts = {len(ops) * i // count for i in range(count + 1)}
        forced = None
        if witness and t == witness['source_task'] and count > 1:
            forced = ops.index(witness['source_op']) + 1
            if 0 < forced < len(ops):
                cuts.add(forced)
                while len(cuts) > count + 1:
                    removable = cuts - {0, len(ops), forced}
                    cuts.remove(min(removable, key=lambda cut: (abs(cut - forced), cut)))
        cuts = sorted(cuts)
        for start, stop in zip(cuts, cuts[1:]):
            block = ops[start:stop]
            b = len(blocks); blocks.append(block); preferred.append(owner[t]); task_blocks[t].append(b)
            if t not in selected: fixed[b] = owner[t]
    extra = [set() for _ in blocks]
    for seq in incumbent['core_schedules']:
        outside = [task_blocks[t][0] for t in seq if t not in selected]
        for a, b in zip(outside, outside[1:]): extra[b].add(a)
    if time.perf_counter() >= deadline: raise TimeoutError('region construction deadline')
    model = _raw_model(ir, incumbent, blocks, fixed, preferred, dict(
        scope=scope, primary_tasks=primary, released_halo_tasks=halo, selected_tasks=sorted(selected),
        selected_operations=sum(len(nodes[t]) for t in selected), target_active_count=target_count,
        witness=witness, partition='topological atoms inside selected incumbent Tasks',
        grouping_scope='common fixed fine partition; cores and ready order jointly free'), extra)
    model['metadata']['active_count'] = len(model['active'])
    model['metadata']['protected_pairs'] = []
    if witness:
        a, b = (model['mapping'][witness[k]] for k in ('source_op', 'target_op'))
        if a != b: model['metadata']['protected_pairs'].append((a, b))
    model['metadata']['build_seconds'] = time.perf_counter() - started
    return model


def model_with_blocks(ir, model, blocks, preferred=None):
    """Rebuild a coarse complete partition, preserving every old fixed block.

    Active blocks may be merged or split. Old fixed blocks must occur unchanged;
    their original relative core order constraints are transferred. Input block
    IDs may be reordered; metadata.input_block_to_model gives the permutation.
    """
    op_new = {op: b for b, block in enumerate(blocks) for op in block}
    fixed, owners, extra = {}, [], [set() for _ in blocks]
    old_to_new = {}
    for old, core in model['fixed'].items():
        new_ids = {op_new[o] for o in model['blocks'][old]}
        if len(new_ids) != 1: raise ValueError('cannot split a fixed outside block')
        new = next(iter(new_ids))
        if set(blocks[new]) != set(model['blocks'][old]): raise ValueError('cannot merge a fixed outside block')
        fixed[new] = core; old_to_new[old] = new
    for old, new in old_to_new.items():
        extra[new].update(old_to_new[p] for p in model['preds'][old] if p in old_to_new)
    pref_owner = (preferred or {}).get('owner', {})
    for b, block in enumerate(blocks):
        votes = Counter(model['preferred_owner'][model['mapping'][o]] for o in block)
        owners.append(pref_owner.get(b, max(votes, key=lambda c: (votes[c], -c))))
    return _raw_model(ir, model['incumbent'], blocks, fixed, owners, dict(model['metadata']), extra)


def plan_from_assignment(model, owners, starts=None, order=None):
    if order is None:
        if starts is None: order = _topo(model['preds'])[0]
        else: order = sorted(range(len(model['blocks'])), key=lambda b: (starts[b], b))
    if sorted(order) != list(range(len(model['blocks']))): raise ValueError('order is not a full block permutation')
    schedules = [[] for _ in range(model['ncores'])]
    for b in order:
        c = owners[b]
        if not 0 <= c < model['ncores']: raise ValueError('invalid block core')
        if b in model['fixed'] and c != model['fixed'][b]: raise ValueError('fixed outside core changed')
        schedules[c].append(b)
    mapping = model.get('mapping') or {op: b for b, block in enumerate(model['blocks']) for op in block}
    return dict(node_to_subgraph={o: mapping[int(o)] for o in model['incumbent']['node_to_subgraph']},
                core_schedules=schedules)


def score_plan(ir, plan):
    """Recompute durations and full plan precedence for this exact candidate."""
    validate_plan(ir, plan)
    _, owner, nodes, _, _ = view(ir, plan)
    tasks = sorted(owner)
    index = {t: b for b, t in enumerate(tasks)}
    blocks = [nodes[t] for t in tasks]
    extra = [set() for _ in blocks]
    for seq in plan['core_schedules']:
        for a, b in zip(seq, seq[1:]): extra[index[b]].add(index[a])
    model = _raw_model(ir, plan, blocks, {}, [owner[t] for t in tasks], extra_preds=extra)
    owners = model['preferred_owner']
    task_to_new = {t: model['mapping'][nodes[t][0]] for t in tasks}
    previous = {task_to_new[b]: task_to_new[a] for seq in plan['core_schedules'] for a, b in zip(seq, seq[1:])}
    ends, starts = [0.] * len(blocks), [0.] * len(blocks)
    for b in _topo(model['preds'])[0]:
        release = max((ends[p] + (1000 if owners[p] != owners[b] else 0)
                       for p in model['preds'][b]), default=0.)
        start = max(release, ends[previous[b]] + 100 if b in previous else 0.)
        starts[b] = start; ends[b] = start + model['durations'][b]
    makespan = max(ends, default=0.)
    return dict(proxy=makespan, makespan_proxy=makespan, components=dict(
        task_count=len(blocks), root_input_bytes=sum(model['root_bytes']),
        boundary_input_bytes=sum(model['boundary_bytes']),
        estimated_copy_bytes=2 * sum(model['boundary_bytes']),
        summed_task_duration=sum(model['durations']),
        same_core_switch_cycles=100 * sum(max(0, len(seq) - 1) for seq in plan['core_schedules']),
        cross_core_dependency_edges=sum(owners[p] != owners[b] for b, ps in enumerate(model['preds']) for p in ps)),
        scope=model['metadata']['proxy_scope'])


def solve_beam(ir, model, width=8, deadline=float('inf'), preferred=None):
    """Branch over ready Task choices AND core choices; return best full plan.

    A width-one valid warm schedule is built first. If the wider beam hits its
    deadline it returns that complete fallback, never a partial assignment.
    """
    started = time.perf_counter(); count = len(model['blocks']); ncores = model['ncores']
    preds, durations, fixed = model['preds'], model['durations'], model['fixed']
    topo, succ = _topo(preds)
    tail = [0.] * count
    for b in reversed(topo): tail[b] = durations[b] + max((tail[c] for c in succ[b]), default=0.)
    preferred = preferred or {}
    pref_owner = preferred.get('owner', {})
    pref_position = {b: i for i, b in enumerate(preferred.get('order', []))}
    active = set(model['active'])

    def candidate_cores(b):
        if b in fixed: return [fixed[b]]
        pc = pref_owner.get(b, model['preferred_owner'][b])
        return [pc] + [c for c in range(ncores) if c != pc]

    def times(b, c, owners, ends, available, lengths):
        release = max((ends[p] + (1000 if owners[p] != c else 0) for p in preds[b]), default=0.)
        start = max(release, available[c] + (100 if lengths[c] else 0))
        return start, start + durations[b]

    # Complete legal fallback in one pass; no obsolete trace times used.
    owners, ends, available, lengths = [-1] * count, [0.] * count, [0.] * ncores, [0] * ncores
    degree = list(map(len, preds)); ready = {b for b in range(count) if not degree[b]}; order = []
    while ready:
        b = min(ready, key=lambda x: (-tail[x], pref_position.get(x, count), x))
        choices = [(times(b, c, owners, ends, available, lengths)[1], c) for c in candidate_cores(b)]
        end, c = min(choices); owners[b] = c; ends[b] = end; available[c] = end; lengths[c] += 1
        ready.remove(b); order.append(b)
        for child in succ[b]:
            degree[child] -= 1
            if not degree[child]: ready.add(child)
    fallback = (owners, ends, available, lengths, degree, ready, order)
    fallback_plan = plan_from_assignment(model, owners, order=order); validate_plan(ir, fallback_plan)
    best, best_value = fallback, max(available, default=0.)
    expansions, branch_task_steps, timed_out = 0, 0, time.perf_counter() >= deadline
    if width > 1 and time.perf_counter() < deadline:
        states = [([-1] * count, [0.] * count, [0.] * ncores, [0] * ncores,
                   list(map(len, preds)), {b for b in range(count) if not preds[b]}, [])]
        for step in range(count):
            if time.perf_counter() >= deadline:
                timed_out = True; break
            candidates = []
            for si, (owners, ends, available, lengths, degree, ready, order) in enumerate(states):
                # Distinct ready tasks are genuinely explored. Prefer two free
                # tasks plus the earliest outside task, with a hard small cap.
                by_tail = sorted(ready, key=lambda x: (-tail[x], pref_position.get(x, count), x))
                choices = [b for b in by_tail if b in active][:2]
                outside = [b for b in by_tail if b not in active]
                if outside: choices.append(outside[0])
                if not choices: choices = by_tail[:3]
                branch_task_steps += len(choices) > 1
                for b in choices:
                    for c in candidate_cores(b):
                        start, end = times(b, c, owners, ends, available, lengths)
                        new_available = available.copy(); new_available[c] = end
                        # Current frontier lower estimate stops the beam from
                        # preferring short irrelevant tasks merely for low end.
                        bound = max(max(new_available), end + tail[b] - durations[b])
                        for r in ready - {b}:
                            earliest = min(times(r, rc, owners, ends, new_available,
                                           [lengths[k] + (k == c) for k in range(ncores)])[0]
                                           for rc in candidate_cores(r))
                            bound = max(bound, earliest + tail[r])
                        preference = 0 if c == pref_owner.get(b, model['preferred_owner'][b]) else 1
                        candidates.append(((bound, max(new_available), sum(new_available), preference,
                                            pref_position.get(b, count), b, c), si, b, c, end))
                        expansions += 1
            next_states, signatures = [], set()
            for rank, si, b, c, end in sorted(candidates, key=lambda entry: entry[0]):
                owners, ends, available, lengths, degree, ready, order = states[si]
                # Full sequence matters only per core; equivalent interleavings
                # are deduplicated while alternative core orders survive.
                signature = tuple(tuple(x for x in order + [b] if (c if x == b else owners[x]) == core)
                                  for core in range(ncores))
                if signature in signatures: continue
                signatures.add(signature)
                new_owners = owners.copy(); new_owners[b] = c
                new_ends = ends.copy(); new_ends[b] = end
                new_available = available.copy(); new_available[c] = end
                new_lengths = lengths.copy(); new_lengths[c] += 1
                new_degree = degree.copy(); new_ready = ready - {b}
                for child in succ[b]:
                    new_degree[child] -= 1
                    if not new_degree[child]: new_ready.add(child)
                next_states.append((new_owners, new_ends, new_available, new_lengths,
                                    new_degree, new_ready, order + [b]))
                if len(next_states) >= width: break
            if not next_states: raise ValueError('beam lost all legal frontier states')
            states = next_states
        else:
            beam_best = min(states, key=lambda state: (max(state[2]), sum(state[2])))
            if max(beam_best[2]) <= best_value: best = beam_best; best_value = max(beam_best[2])
    owners, _, _, _, _, _, order = best
    result = plan_from_assignment(model, owners, order=order); validate_plan(ir, result)
    return result, dict(method='ready_task_core_beam', width=width, proxy=best_value,
                        owners=owners, assignment=owners, order=order, expansions=expansions,
                        ready_task_branch_steps=branch_task_steps, timed_out=timed_out,
                        returned_complete_fallback=best is fallback,
                        seconds=time.perf_counter() - started)
