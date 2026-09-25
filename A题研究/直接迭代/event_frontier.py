"""Gap-inserting operation placement and dependency-sealed P1 Task formation.

The search changes ownership, pipe order and Task boundaries together. Each
yield is a complete legal plan; generation never recompiles the official graph.
Timing/traffic estimates are ranking heuristics, not official simulation.
"""
from bisect import bisect_right
from collections import defaultdict
import heapq
import time

from common_run import validate_plan
from unified_structure import Structure


class Calendar:
    def __init__(self):
        self.ends = []
        self.intervals = []

    def slot(self, release, duration):
        index = bisect_right(self.ends, release)
        start = release
        while index < len(self.intervals):
            a, b, _ = self.intervals[index]
            if start + duration <= a:
                break
            start = max(start, b)
            index += 1
        return start, index

    def insert(self, start, duration, op, index):
        self.ends.insert(index, start + duration)
        self.intervals.insert(index, (start, start + duration, op))


def ordered_ready(structure, ordering):
    ir = structure.ir
    degree = {o: len(ir.predecessors[o]) for o in structure.topo}
    def key(o):
        if ordering == 'stable':
            return (o, o)
        if ordering == 'release':
            release = sum(structure.tensors[t].size for t in structure.inputs[o])
            output = sum(structure.tensors[t].size for t in structure.outputs[o])
            return (-structure.tail[o] - (release-output)/60, o)
        return (-structure.tail[o], o)
    queue = [key(o) for o in structure.topo if not degree[o]]
    heapq.heapify(queue)
    while queue:
        _, o = heapq.heappop(queue)
        yield o
        for c in ir.successors[o]:
            degree[c] -= 1
            if not degree[c]:
                heapq.heappush(queue, key(c))


def insertion_schedule(structure, cores, ordering='critical', communication=1.,
                       locality=0., fixed=None, deadline=float('inf')):
    """Insert compute into idle gaps instead of appending every ready operation.

    Transfer routes are deduplicated by tensor and destination. A same-core
    producer is local; root inputs charge a private read cursor. Shared DDR,
    FIFO and compiler memory reuse remain the official evaluator's job.
    """
    ir = structure.ir
    calendars = defaultdict(Calendar)
    owners, starts, ends = {}, {}, {}
    routes, input_cursor = {}, [0.] * cores
    programmed_bytes = 0
    for o in ordered_ready(structure, ordering):
        if time.monotonic() >= deadline:
            raise TimeoutError('insertion placement deadline')
        duration = max(1, ir.ops[o]['cycles'])
        pipe = ir.ops[o]['pipe']
        choices = []
        for core in ([fixed[o]] if fixed is not None else range(cores)):
            release = max((ends[p] for p in ir.predecessors[o]), default=0.)
            new_routes, new_bytes, cursor = {}, 0, input_cursor[core]
            for tid in structure.inputs[o]:
                tensor = structure.tensors[tid]
                sources = {owners[p] for p in tensor.producers}
                # Multiple-producer tensors still wait for every producer.
                if tensor.producers:
                    source_end = max(ends[p] for p in tensor.producers)
                    release = max(release, source_end)
                    if sources == {core}:
                        continue
                    key = (tid, core)
                    if key not in routes:
                        ready = source_end + communication * (500 + 2*tensor.size/60)
                        new_routes[key] = ready
                        new_bytes += 2*tensor.size
                    else:
                        ready = routes[key]
                else:
                    key = (tid, core)
                    if key not in routes:
                        cursor += communication * max(1, tensor.size/60)
                        ready = cursor
                        new_routes[key] = ready
                        new_bytes += tensor.size
                    else:
                        ready = routes[key]
                release = max(release, ready)
            calendar = calendars[core, pipe]
            start, index = calendar.slot(release, duration)
            end = start + duration
            # Byte tie-break discourages gratuitous replication; locality is
            # a tunable proxy tradeoff, never a change to official bandwidth.
            rank = (end + locality*new_bytes/60, new_bytes, end, core)
            choices.append((rank, core, start, index, new_routes, cursor, new_bytes))
        _, core, start, index, copied, cursor, new_bytes = min(choices, key=lambda x:x[0])
        calendars[core, pipe].insert(start, duration, o, index)
        owners[o], starts[o], ends[o] = core, start, start + duration
        routes.update(copied)
        input_cursor[core] = cursor
        programmed_bytes += new_bytes
    # An insertion can place a later-chosen op before an earlier-chosen op.
    # Encode the resulting pipe order together with all original dependencies.
    preds = {o: set(ir.predecessors[o]) for o in structure.topo}
    for calendar in calendars.values():
        seq = [x[2] for x in calendar.intervals]
        for a, b in zip(seq, seq[1:]):
            preds[b].add(a)
    successors = {o: [] for o in structure.topo}
    for o, ps in preds.items():
        for p in ps:
            successors[p].append(o)
    degree = {o: len(ps) for o, ps in preds.items()}
    ready = [(starts[o], -structure.tail[o], o) for o in structure.topo if not degree[o]]
    heapq.heapify(ready)
    order = []
    while ready:
        _, _, o = heapq.heappop(ready)
        order.append(o)
        for c in successors[o]:
            degree[c] -= 1
            if not degree[c]:
                heapq.heappush(ready, (starts[c], -structure.tail[c], c))
    if len(order) != len(structure.topo):
        raise ValueError('insertion schedule dependency cycle')
    return owners, order, dict(proxy_finish=max(ends.values(), default=0),
                              proxy_copy_bytes=programmed_bytes,
                              calendar_intervals=sum(len(c.intervals) for c in calendars.values()))


def sealed_tasks(ir, order, owners, cores, work_cap=float('inf')):
    """Keep a Task open until a scheduled outside consumer uses its output.

    Unlike run-length compression, unrelated work on other cores does not force
    a cut. Unlike unrestricted same-core fusion, a Task that has emitted a
    dependency is never extended again. This avoids folding a successor back
    into an ancestor and preserves the quotient DAG and per-core Task order.
    """
    mapping, blocks, block_owner, work = {}, [], [], []
    current, sealed = [None] * cores, set()
    schedules = [[] for _ in range(cores)]
    for o in order:
        core = owners[o]
        task = current[core]
        pipe, cost = ir.ops[o]['pipe'], max(1, ir.ops[o]['cycles'])
        if task is None or task in sealed or work[task][pipe] + cost > work_cap:
            if task is not None:
                sealed.add(task)
            task = len(blocks)
            blocks.append([]); block_owner.append(core); work.append(defaultdict(int))
            current[core] = task; schedules[core].append(task)
        for p in ir.predecessors[o]:
            parent = mapping[p]
            if parent != task:
                sealed.add(parent)
        mapping[o] = task
        blocks[task].append(o); work[task][pipe] += cost
    plan = {'node_to_subgraph': {str(o): mapping[o] for o in order},
            'core_schedules': schedules}
    validate_plan(ir, plan)
    return plan, blocks


def operation_plan(ir, order, owners, cores):
    plan = {'node_to_subgraph': {str(o): i for i, o in enumerate(order)},
            'core_schedules': [[] for _ in range(cores)]}
    for i, o in enumerate(order):
        plan['core_schedules'][owners[o]].append(i)
    validate_plan(ir, plan)
    return plan


def candidates(ir, problem, cores, parent=None, deadline=float('inf')):
    """Stream different structural proposals without a bulk generation barrier."""
    structure = Structure(ir)
    specs = [('critical', .25, 0.), ('critical', 1., 0.),
             ('release', 1., .25), ('stable', 1., 0.),
             ('critical', 2., .25), ('release', .25, 0.)]
    if parent is not None:
        task_core = {task: core for core, seq in enumerate(parent['core_schedules']) for task in seq}
        fixed = {int(o): task_core[task] for o, task in parent['node_to_subgraph'].items()}
        specs = [('parent', 1., 0.)] + specs
    else:
        fixed = None
    for index, (ordering, communication, locality) in enumerate(specs):
        owner, order, info = insertion_schedule(structure, cores,
            'critical' if ordering == 'parent' else ordering, communication, locality,
            fixed if ordering == 'parent' else None, deadline)
        meta = dict(family='event_frontier', ordering=ordering, communication=communication,
                    locality=locality, fixed_assignment=ordering == 'parent', **info)
        if problem != 1:
            yield dict(name=f'insert_{index}_{ordering}', plan=operation_plan(ir,order,owner,cores),metadata=meta)
            continue
        cap = max(1000, max(ir.total_work_m,ir.total_work_v)/max(1,cores*4))
        for limit in (float('inf'), cap):
            plan, blocks = sealed_tasks(ir, order, owner, cores, limit)
            label = 'open' if limit == float('inf') else 'capped'
            yield dict(name=f'sealed_{index}_{ordering}_{label}',plan=plan,
                       metadata=dict(meta,task_count=len(blocks),cap=None if label=='open' else limit))
            # Global reassignment is paired with this new partition. It is not
            # confined to the old bottleneck's ownership or outside Task order.
            from p1_selective import _toposort_blocks, block_views, _assign
            sorted_blocks = _toposort_blocks(ir,blocks,order)
            view = block_views(ir,sorted_blocks)
            reassigned, proxy = _assign(ir,sorted_blocks,view,cores,cores,'eft')
            yield dict(name=f'sealed_{index}_{ordering}_{label}_reassign',plan=reassigned,
                       metadata=dict(meta,task_count=len(blocks),global_reassignment=True,task_proxy=proxy))
