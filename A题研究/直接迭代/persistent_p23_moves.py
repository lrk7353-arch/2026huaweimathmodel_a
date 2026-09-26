"""Lazy, observation-driven P23 regional placement/order/FIFO actions.

Each next() constructs at most one complete proposal. The caller owns official
evaluation, parent retention and a process-level generation deadline. `seconds`
limits accumulated generator work, excluding time suspended at yield; Python
cooperative checks cannot preempt the optional official observation compiler.
"""
from collections import Counter, defaultdict
from contextlib import contextmanager
import heapq
import json
import math
import time

from common_run import validate_plan
from advanced_solver.trace_refine import _tensor_views, _plan_assignment, _topology, _runs_plan
from p23_data_refine import partition_copy_bytes
from wait_observation import observe


class _WorkBudget:
    def __init__(self, seconds):
        self.remaining = seconds
        self.deadline = None

    @contextmanager
    def segment(self):
        if self.remaining <= 0:
            raise TimeoutError('persistent P23 generation budget exhausted')
        start = time.monotonic()
        self.deadline = start + self.remaining
        try:
            yield
        finally:
            self.remaining -= time.monotonic() - start

    def check(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('persistent P23 generation deadline')


def _grow(ir, seed, owners, views, maximum, work_cap, check):
    """Connected same-core chain; tensor affinity computed only as visited."""
    source = owners[seed]
    todo = [(0, seed)]
    queued, region, work = {seed}, [], Counter()
    while todo and len(region) < maximum:
        check()
        _, op = heapq.heappop(todo)
        pipe, cost = ir.ops[op]['pipe'], max(1, ir.ops[op]['cycles'])
        if region and work[pipe] + cost > work_cap:
            continue
        region.append(op)
        work[pipe] += cost
        for neighbour in sorted(set(ir.predecessors[op]) | set(ir.successors[op])):
            check()
            if neighbour in queued or owners[neighbour] != source:
                continue
            queued.add(neighbour)
            affinity = sum(ir.tensors[t]['size'] for t in views[2][op] & views[2][neighbour])
            heapq.heappush(todo, (-affinity, neighbour))
    return region


def _boundary(ir, members, order_rank, maximum, check):
    """Allow neighbouring producers/consumers to participate in ordering."""
    region = set(members)
    neighbours = set()
    for op in members:
        check()
        neighbours.update(ir.predecessors[op])
        neighbours.update(ir.successors[op])
    for op in sorted(neighbours - region, key=order_rank.__getitem__):
        if len(region) >= maximum:
            break
        region.add(op)
    return region


def _repair_order(ir, owners, order, region, obs, capacity, views, check,
                  priority=None, window=24):
    """Repair a related region while retaining all outside observed order.

    Added ordering edges constrain only this construction, not the official
    graph. Copy time/residency are ranking proxies, never feasibility proofs.
    """
    priority = priority or {}
    rank = {op: i for i, op in enumerate(order)}
    preds = {op: set(ir.predecessors[op]) for op in order}
    outside = [op for op in order if op not in region]
    for first, second in zip(outside, outside[1:]):
        preds[second].add(first)
    successors = {op: [] for op in order}
    for op, parents in preds.items():
        check()
        for parent in parents:
            successors[parent].append(op)
    degree = {op: len(parents) for op, parents in preds.items()}
    ready = [(priority.get(op, rank[op]), rank[op], op) for op in order if not degree[op]]
    heapq.heapify(ready)
    remaining = Counter((t, owners[o]) for t, readers in views[1].items() for o in readers)
    alive, live, peaks = set(), Counter(), Counter()
    finish, pipes, output = {}, Counter(), []
    while ready:
        check()
        options = [heapq.heappop(ready) for _ in range(min(window, len(ready)))]
        scores = {}
        for prio, old_rank, op in options:
            check()
            core = owners[op]
            release = 0.
            for parent in ir.predecessors[op]:
                crossing = owners[parent] != core
                size = sum(ir.tensors[t]['size'] for t in views[2][op] & views[2][parent])
                release = max(release, finish[parent] + (500 + 2*size/60 if crossing else 0))
            allocated, freed = Counter(), Counter()
            for tensor in views[2][op]:
                pos = ir.tensors[tensor]['pos']
                pos = 'UB' if pos == 'DDR' else pos
                size, key = ir.tensors[tensor]['size'], (tensor, core)
                if key not in alive:
                    allocated[pos] += size
                if remaining[key] - int(op in views[1][tensor]) == 0:
                    freed[pos] += size
            root_bytes = sum(ir.tensors[t]['size'] for t in views[2][op]
                             if not views[0][t] and (t, core) not in alive)
            start = max(release, pipes[core, ir.ops[op]['pipe']]) + root_bytes/60
            pressure = sum(allocated[p] - freed[p] +
                           2*max(0, live[core, p] + allocated[p] - capacity[p]) for p in capacity)/60
            tail = obs['tails'][obs['compute'][op]]
            # Explicit FIFO priority is a bounded ordering request; ordinary
            # regional choices balance release, critical tail and residency.
            forced = prio - old_rank if op in priority else 0
            scores[op] = (forced, start - .35*tail + .5*pressure, old_rank,
                          start, allocated, freed)
        chosen = min(options, key=lambda item: scores[item[2]][:3])
        op = chosen[2]
        for item in options:
            if item != chosen:
                heapq.heappush(ready, item)
        _, _, _, start, allocated, freed = scores[op]
        core = owners[op]
        for pos in capacity:
            peaks[core, pos] = max(peaks[core, pos], live[core, pos] + allocated[pos])
            live[core, pos] += allocated[pos] - freed[pos]
            if live[core, pos] < 0:
                raise ValueError('negative lifetime surrogate')
        for tensor in views[2][op]:
            key = tensor, core
            if op in views[1][tensor]:
                remaining[key] -= 1
            if remaining[key]:
                alive.add(key)
            else:
                alive.discard(key)
        finish[op] = start + max(1, ir.ops[op]['cycles'])
        pipes[core, ir.ops[op]['pipe']] = finish[op]
        output.append(op)
        for child in successors[op]:
            degree[child] -= 1
            if not degree[child]:
                heapq.heappush(ready, (priority.get(child, rank[child]), rank[child], child))
    if len(output) != len(order):
        raise ValueError('regional ordering constraints are cyclic')
    if [op for op in output if op not in region] != outside:
        raise AssertionError('outside observed order changed')
    return output, dict(estimated_finish=max(finish.values(), default=0),
                        surrogate_peak={f'{c}:{p}': n for (c, p), n in peaks.items()})


def _movement_actions(ir, owners, order, obs, views, cores, round_index, check):
    """Yield specifications, not an eagerly generated pool of complete plans."""
    loads = Counter()
    for op in order:
        check()
        loads[owners[op], ir.ops[op]['pipe']] += max(1, ir.ops[op]['cycles'])
    rank = {op: i for i, op in enumerate(order)}
    peak = max(loads.values(), default=1)
    maximum = 32 if round_index % 2 == 0 else 96
    seeds = []
    copies = heapq.nsmallest(4, obs['copies'].items(), key=lambda item: -item[1]['wait_priority'])
    for key, entry in copies:
        check()
        for tensor in entry.get('original_tensors', []):
            readers = views[1][tensor]
            producers = views[0][tensor]
            if producers and readers:
                producer = max(producers, key=lambda o: obs['events'][obs['compute'][o]]['end'])
                foreign = [o for o in readers if owners[o] != owners[producer]]
                if foreign:
                    reader = min(foreign, key=rank.__getitem__)
                    reason = dict(kind='copy_wait', event=list(key), tensor_id=tensor,
                                  wait_priority=entry['wait_priority'])
                    seeds.append((reader, owners[producer], reason))
                    seeds.append((producer, owners[reader], reason))
            elif readers:
                reader_cores = {owners[o] for o in readers}
                if len(reader_cores) > 1:
                    seed = min(readers, key=rank.__getitem__)
                    dest = min(reader_cores - {owners[seed]},
                               key=lambda c: (loads[c, ir.ops[seed]['pipe']], c))
                    seeds.append((seed, dest, dict(kind='shared_input_wait', event=list(key),
                                                  tensor_id=tensor, wait_priority=entry['wait_priority'])))
    hot = heapq.nsmallest(4, order, key=lambda op: (
        obs['slack'][obs['compute'][op]], -ir.ops[op]['cycles'], -rank[op]))
    for op in hot:
        destinations = [c for c in range(cores) if c != owners[op]]
        if destinations:
            dest = min(destinations, key=lambda c: (loads[c, ir.ops[op]['pipe']], c))
            seeds.append((op, dest, dict(kind='critical_compute', event=list(obs['compute'][op]),
                                        slack=obs['slack'][obs['compute'][op]])))
    seen = set()
    for seed, dest, reason in seeds:
        check()
        source = owners[seed]
        members = _grow(ir, seed, owners, views, maximum, max(1, peak/4), check)
        changes = {op: dest for op in members}
        identity = tuple(sorted(changes.items()))
        if identity in seen:
            continue
        seen.add(identity)
        pipe = ir.ops[seed]['pipe']
        moved_work = sum(ir.ops[op]['cycles'] for op in members if ir.ops[op]['pipe'] == pipe)
        excess = max(0, (loads[dest, pipe] + moved_work - loads[source, pipe] + moved_work)/2)
        exchange_seeds = [op for op in hot if owners[op] == dest and ir.ops[op]['pipe'] == pipe]
        if not exchange_seeds:
            exchange_seeds = heapq.nsmallest(1, (op for op in order if owners[op] == dest and
                                                ir.ops[op]['pipe'] == pipe),
                                            key=lambda op: abs(ir.ops[op]['cycles']-excess))
        if excess and exchange_seeds:
            compensating = _grow(ir, exchange_seeds[0], owners, views, max(1, maximum//2), excess, check)
            swapped = dict(changes)
            swapped.update({op: source for op in compensating})
            region = _boundary(ir, set(swapped), rank, 256, check)
            yield dict(changes=swapped, region=region, priority={}, bottleneck=reason,
                       action='related_chain_exchange', seed=seed, compensated=True)
        region = _boundary(ir, set(changes), rank, 256, check)
        yield dict(changes=changes, region=region, priority={}, bottleneck=reason,
                   action='related_chain_move', seed=seed, compensated=False)


def _fifo_actions(ir, owners, order, obs, raw, views, round_index, check):
    """Target a real miss, jointly changing the first reader and follower timing.

    Arrival/eviction labels describe this paid trace. They do not predict that
    the proposed plan hits; only existing useful compute work is reordered.
    """
    rank = {op: i for i, op in enumerate(order)}
    groups = defaultdict(list)
    for key, entry in obs['copies'].items():
        check()
        tensor = entry.get('cache_tensor_id')
        if entry.get('op') == 'COPY_IN' and tensor in ir.tensors:
            groups[tensor].append((key, entry))
    targets = []
    for tensor, reads in groups.items():
        if len({entry['core_id'] for _, entry in reads}) < 2:
            continue
        leader_key, leader = min(reads, key=lambda item: (item[1]['end'], item[1]['start'], item[0]))
        for key, entry in reads:
            if entry.get('cache_hit') or entry['core_id'] == leader['core_id']:
                continue
            targets.append((entry['wait_priority'], tensor, key, entry, leader_key, leader))
    targets.sort(key=lambda row: (-row[0], row[1], row[2]))
    width = 8 if round_index % 2 == 0 else 24
    ancestor_cache = {}
    def ancestors(op):
        if op not in ancestor_cache:
            reached, stack = set(), [op]
            while stack:
                check()
                current = stack.pop()
                if current in reached:
                    continue
                reached.add(current)
                if len(reached) > 512:
                    ancestor_cache[op] = None
                    return None
                stack.extend(ir.predecessors[current])
            ancestor_cache[op] = reached
        return ancestor_cache[op]
    for pressure, tensor, key, follower, leader_key, leader in targets[:3]:
        check()
        leader_readers = [op for op in views[1][tensor] if owners[op] == leader['core_id']]
        follower_readers = [op for op in views[1][tensor] if owners[op] == follower['core_id']]
        if not leader_readers or not follower_readers:
            continue
        first = min(leader_readers, key=rank.__getitem__)
        consumer = min(follower_readers, key=rank.__getitem__)
        lead_ancestors, follow_ancestors = ancestors(first), ancestors(consumer)
        if lead_ancestors is None or follow_ancestors is None:
            continue
        region = {first, consumer}
        priority = {}
        def lift(ops, anchor):
            for op in ops:
                parents = ancestors(op)
                if parents is None:
                    continue
                for parent in parents:
                    if rank[parent] >= anchor:
                        region.add(parent)
                        priority[parent] = min(priority.get(parent, rank[parent]), anchor-.25)
        lift([first], max(0, rank[first]-width))
        concurrent = follower['start'] < leader['end']
        if concurrent:
            useful = []
            blocked = set(views[1][tensor])
            for op in order[rank[consumer]+1:]:
                check()
                if owners[op] != owners[consumer]:
                    continue
                parents = ancestors(op)
                if parents is None or op in follow_ancestors or parents & blocked:
                    continue
                useful.append(op)
                if len(useful) >= width:
                    break
            if not useful:
                continue
            lift(useful, rank[consumer])
            mode = 'first_reader_and_independent_follower_work'
        else:
            lift([consumer], max(0, rank[consumer]-width))
            mode = 'first_reader_and_late_consumer'
        if len(region) > 256:
            continue
        evictions = [event['time'] for event in raw.get('cache_events', [])
                     if tensor in event.get('evicted_tensor_ids', []) and event['time'] <= follower['start']]
        reason = dict(kind='fifo_read_window', tensor_id=tensor, event=list(key),
                      leader_event=list(leader_key), leader_end=leader['end'],
                      follower_start=follower['start'], observed_evictions=evictions,
                      concurrent_cold_miss=concurrent, wait_priority=pressure)
        yield dict(changes={}, region=region, priority=priority, bottleneck=reason,
                   action=mode, seed=consumer, compensated=False)
        moving = _grow(ir, consumer, owners, views, min(8, width),
                       max(1, sum(ir.ops[o]['cycles'] for o in follower_readers)), check)
        changes = {op: owners[first] for op in moving}
        joined = _boundary(ir, region | set(moving), rank, 256, check)
        if len(joined) <= 256:
            yield dict(changes=changes, region=joined, priority=priority, bottleneck=reason,
                       action=mode+'_reader_region_move', seed=consumer, compensated=False)


def iter_candidates(ir, plan, raw, cores, round_index=0, observation=None, seconds=12):
    """Yield complete joint plans; preserve iterator state between paid calls."""
    if cores not in range(1, 6) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('positive generation seconds and 1..5 cores required')
    problem = raw.get('problem', 3 if raw.get('cache_mode') else 2)
    if problem not in (2, 3):
        raise ValueError('persistent_p23_moves supports P2/P3')
    work = _WorkBudget(seconds)
    with work.segment():
        owners = _plan_assignment(ir, plan, cores)
        obs = observation if observation is not None else observe(ir, plan, raw, problem)
        work.check()
        if obs['assignment'] != owners or obs['summary']['makespan'] != raw['makespan']:
            raise ValueError('cached observation does not match parent placement/time')
        views = _tensor_views(ir)
        order = _topology(ir, {op: (obs['events'][obs['compute'][op]]['start'],
                                  obs['events'][obs['compute'][op]]['end'], op) for op in ir.compute_ids})
        base_bytes = partition_copy_bytes(ir, owners, views)
        streams = [_movement_actions(ir, owners, order, obs, views, cores, round_index, work.check)]
        if problem == 3:
            streams.append(_fifo_actions(ir, owners, order, obs, raw, views, round_index, work.check))
        active, cursor, sequence = list(streams), 0, 0
        seen = {json.dumps(plan, ensure_ascii=False, separators=(',', ':'))}
    while active:
        with work.segment():
            work.check()
            stream_index = cursor % len(active)
            stream = active[stream_index]
            try:
                action = next(stream)
            except StopIteration:
                active.pop(stream_index)
                continue
            cursor = (stream_index + 1) % len(active)
            changed = dict(owners)
            changed.update(action['changes'])
            if any(owners[o] != changed[o] for o in order if o not in action['region']):
                raise AssertionError('movement escaped repair region')
            new_order, diagnostics = _repair_order(ir, changed, order, action['region'], obs,
                raw['capacity_bytes'], views, work.check, action['priority'],
                window=24 if round_index % 2 == 0 else 48)
            candidate_plan = _runs_plan(ir, new_order, changed, cores, max_run_ops=1)
            validate_plan(ir, candidate_plan)
            work.check()
            signature = json.dumps(candidate_plan, ensure_ascii=False, separators=(',', ':'))
            if signature in seen:
                continue
            seen.add(signature)
            changes = [dict(op=op, from_core=owners[op], to_core=core)
                       for op, core in sorted(action['changes'].items()) if owners[op] != core]
            metadata = dict(family='persistent_p23_joint', action=action['action'],
                bottleneck=action['bottleneck'], region_ops=sorted(action['region']),
                moved_ops=len(changes), changes=changes, compensated=action['compensated'],
                round_index=round_index, parent_makespan=raw['makespan'],
                observation_reused=observation is not None,
                outside_assignment_preserved=True, outside_observed_order_preserved=True,
                global_reencoding=True, ordering='regional_pipeline_lifetime',
                partition_copy_delta=partition_copy_bytes(ir, changed, views)-base_bytes,
                changed_positions=sum(a != b for a, b in zip(order, new_order)),
                limitations='observed wait, COPY and lifetime proxies; no guaranteed speedup or cache hit',
                **diagnostics)
            work.check()
            sequence += 1
            value = dict(name=f'persistent_p23_r{round_index}_{sequence}_{action["action"]}',
                         plan=candidate_plan, metadata=metadata)
        # Official evaluation while suspended here does not consume the local
        # generation allowance. The outer controller still charges global time.
        yield value
