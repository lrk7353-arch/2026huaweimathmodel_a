"""Shared tensor hypergraph and multiresolution, scene-aware candidate construction.

Costs are ranking heuristics, never feasibility certificates or official times.
Original operators, tensor sizes and hardware settings are not modified.
"""
from collections import defaultdict
from dataclasses import dataclass
import heapq
import math
import time

from common_run import validate_plan


@dataclass(frozen=True)
class Tensor:
    id: int
    size: int
    pos: str
    producers: tuple
    consumers: tuple
    output: bool


class Structure:
    def __init__(self, ir):
        self.ir = ir
        eligible = set(ir.compute_ids)
        producers, consumers = defaultdict(set), defaultdict(set)
        for e in ir.graph['edges']:
            a, b = e['source'], e['target']
            if a in ir.ops:
                producers[b].add(a)
            else:
                consumers[a].add(b)
        self.tensors = {}
        self.inputs = {o: [] for o in eligible}
        self.outputs = {o: [] for o in eligible}
        self.affinity = defaultdict(int)
        for tid, t in ir.tensors.items():
            ps, cs = tuple(sorted(producers[tid] & eligible)), tuple(sorted(consumers[tid] & eligible))
            if not ps and not cs:
                continue
            output = bool(ps) and (not cs or any(ir.ops[o]['op'] == 'COPY_OUT' for o in consumers[tid]))
            self.tensors[tid] = Tensor(tid, t['size'], t['pos'], ps, cs, output)
            for o in ps:
                self.outputs[o].append(tid)
            for o in cs:
                self.inputs[o].append(tid)
            for a in ps:
                for b in cs:
                    self.affinity[a, b] += t['size']
        degree = {o: len(ir.predecessors[o]) for o in eligible}
        ready = [o for o, d in degree.items() if not d]
        heapq.heapify(ready)
        self.topo = []
        self.earliest = {}
        while ready:
            o = heapq.heappop(ready)
            self.topo.append(o)
            self.earliest[o] = max((self.earliest[p] + ir.ops[p]['cycles'] for p in ir.predecessors[o]), default=0)
            for c in ir.successors[o]:
                degree[c] -= 1
                if not degree[c]:
                    heapq.heappush(ready, c)
        if len(self.topo) != len(eligible):
            raise ValueError('compute cycle')
        self.tail = {}
        for o in reversed(self.topo):
            self.tail[o] = max(1, ir.ops[o]['cycles']) + max((self.tail[c] for c in ir.successors[o]), default=0)
        total = max(1, ir.total_work_m + ir.total_work_v)
        self.profile = dict(operations=len(eligible), components=len(ir.components),
            dominant_fraction=max((c.compute_work for c in ir.components), default=0) / total,
            mean_cycles=total / max(1, len(eligible)),
            minor_pipe_fraction=min(ir.total_work_m, ir.total_work_v) / total,
            compute_floor_work=max(ir.total_work_m, ir.total_work_v),
            shared_input_bytes=sum(t.size for t in self.tensors.values() if not t.producers and len(t.consumers) > 1))

    def regions(self, cores, scale=2, family='branch', deadline=float('inf')):
        """Remove a connected ready frontier: every contraction stays acyclic.

        Branch cuts become optional below communication/synchronization scale.
        Wavefront restricts a region's dependency-time span; affinity grows along
        large tensors. Different scales are retained, not declared equivalent.
        """
        ir = self.ir
        target = max(1000, self.profile['compute_floor_work'] / max(1, cores * scale))
        degree = {o: len(ir.predecessors[o]) for o in self.topo}
        ready = {o for o in self.topo if not degree[o]}
        queue = [(-self.tail[o], o) for o in ready]
        heapq.heapify(queue)
        blocks = []
        while ready:
            if time.monotonic() >= deadline:
                raise TimeoutError('region generation deadline')
            while queue and queue[0][1] not in ready:
                heapq.heappop(queue)
            root = heapq.heappop(queue)[1]
            options, inside, block, work = {root}, set(), [], [0, 0]
            minimum = min(target, max(1000, sum(self.tensors[t].size for t in self.inputs[root]) / 30))
            while options:
                def priority(o):
                    shared = sum(self.affinity[p, o] for p in ir.predecessors[o] if p in inside)
                    return (-shared if family == 'affinity' else 0, -self.tail[o], o)
                op = min(options, key=priority)
                options.remove(op)
                pipe = 0 if ir.ops[op]['pipe'] == 'PIPE_M' else 1
                if block and work[pipe] + ir.ops[op]['cycles'] > target:
                    continue
                if family == 'wavefront' and block and self.earliest[op] - self.earliest[root] > target / 2:
                    continue
                ready.remove(op)
                block.append(op)
                inside.add(op)
                work[pipe] += max(1, ir.ops[op]['cycles'])
                newly_ready = []
                for child in ir.successors[op]:
                    degree[child] -= 1
                    if not degree[child]:
                        ready.add(child)
                        heapq.heappush(queue, (-self.tail[child], child))
                        newly_ready.append(child)
                if family == 'branch' and max(work) >= minimum:
                    if len(ir.successors[op]) == 1:
                        options.update(c for c in newly_ready if len(ir.predecessors[c]) == 1)
                else:
                    options.update(newly_ready)
            blocks.append(block)
        return blocks

    def block_view(self, blocks):
        ir = self.ir
        owner = {o: b for b, ops in enumerate(blocks) for o in ops}
        if set(owner) != set(ir.compute_ids) or sum(map(len, blocks)) != len(owner):
            raise ValueError('regions must partition compute operators')
        pred = [set() for _ in blocks]
        succ = [set() for _ in blocks]
        work, duration, inputs, outputs = [], [], [], []
        for b, ops in enumerate(blocks):
            w = [0, 0]
            ends = {}
            for o in ops:
                w[0 if ir.ops[o]['pipe'] == 'PIPE_M' else 1] += max(1, ir.ops[o]['cycles'])
                ends[o] = max(1, ir.ops[o]['cycles']) + max((ends[p] for p in ir.predecessors[o] if p in ends), default=0)
                for p in ir.predecessors[o]:
                    if owner[p] != b:
                        pred[b].add(owner[p])
            work.append(w)
            duration.append(max(w + list(ends.values()) + [0]))
            inputs.append(set(t for o in ops for t in self.inputs[o]))
            outputs.append(set(t for o in ops for t in self.outputs[o]))
        for b, ps in enumerate(pred):
            for p in ps:
                succ[p].add(b)
        return dict(owner=owner, pred=pred, succ=succ, work=work, duration=duration, inputs=inputs, outputs=outputs)

    def block_order(self, blocks, view, ordering='critical'):
        degree = list(map(len, view['pred']))
        # Shared logical input, not the original COPY source DDR id.
        anchor = []
        for ts in view['inputs']:
            reusable = [t for t in ts if not self.tensors[t].producers and self.tensors[t].size <= 1048576]
            anchor.append(min(reusable, key=lambda t: (-self.tensors[t].size * len(self.tensors[t].consumers), t)) if reusable else -1)
        def key(b):
            critical = max(self.tail[o] for o in blocks[b])
            if ordering == 'cache_window':
                return (anchor[b], -critical, b)
            release = sum(self.tensors[t].size for t in view['inputs'][b]) - sum(self.tensors[t].size for t in view['outputs'][b])
            return (-(critical + (release / 60 if ordering == 'residency' else 0)), 0, b)
        queue = [key(b) for b, d in enumerate(degree) if not d]
        heapq.heapify(queue)
        order = []
        while queue:
            b = heapq.heappop(queue)[-1]
            order.append(b)
            for c in sorted(view['succ'][b]):
                degree[c] -= 1
                if not degree[c]:
                    heapq.heappush(queue, key(c))
        if len(order) != len(blocks):
            raise ValueError('region contraction has a cycle')
        return order

    def assign(self, blocks, scene, cores, ordering='critical', communication=1.0,
               residency=0.0, fixed=None, deadline=float('inf')):
        """Joint block ownership and legal priorities, with scene-specific costs.

        P2/P3 estimate every op on separate M/V cursors within each placement
        choice. Liveness is tracked across regions; P3 discounts reusable reads
        only for ranking. It does not claim FIFO hits (official evaluation does).
        """
        ir = self.ir
        view = self.block_view(blocks)
        order = self.block_order(blocks, view, ordering)
        fixed = fixed or {}
        assignment, finish, bfinish = {}, {}, {}
        pipes = [[0., 0.] for _ in range(cores)]
        available = [0.] * cores
        readers, routes = {}, {}
        resident = [set() for _ in range(cores)]
        counts = {t: len(v.consumers) for t, v in self.tensors.items()}
        byte_total, shared_reads = 0, defaultdict(set)
        task_count = [0] * cores
        for b in order:
            if time.monotonic() >= deadline:
                raise TimeoutError('assignment deadline')
            options = []
            for core in ([fixed[b]] if b in fixed else range(cores)):
                local_ends, local_readers, local_routes = {}, {}, {}
                ptime = pipes[core].copy()
                transferred, pressure, new_reads = 0, 0., []
                if scene == 1:
                    release = max((bfinish[p] + (1000 if assignment[blocks[p][0]] != core else 0) for p in view['pred'][b]), default=0)
                    start = max(available[core] + (100 if task_count[core] else 0), release)
                    reads = {t for t in view['inputs'][b] if not self.tensors[t].producers or any(view['owner'][p] != b for p in self.tensors[t].producers)}
                    writes = {t for t in view['outputs'][b] if self.tensors[t].output or any(view['owner'][c] != b for c in self.tensors[t].consumers)}
                    transferred = sum(self.tensors[t].size for t in reads | writes)
                    end = start + view['duration'][b] + communication * transferred / 60
                    local_ends = {o: end for o in blocks[b]}
                    ptime = [end, end]
                else:
                    for o in blocks[b]:
                        pipe = 0 if ir.ops[o]['pipe'] == 'PIPE_M' else 1
                        ready = ptime[pipe]
                        for p in ir.predecessors[o]:
                            ready = max(ready, local_ends[p] if p in local_ends else finish[p])
                        for tid in self.inputs[o]:
                            t = self.tensors[tid]
                            sources = {assignment[p] for p in t.producers if p in assignment}
                            if any(p in local_ends for p in t.producers):
                                sources.add(core)
                            keys = [(tid, src, core) for src in sources if src != core]
                            if not t.producers:
                                keys = [(tid, -1, core)]
                            for key in keys:
                                previous = local_routes.get(key, routes.get(key))
                                if previous is None:
                                    release = max((local_ends.get(p, finish.get(p, 0)) for p in t.producers), default=0)
                                    bandwidth = 250 if scene == 3 and shared_reads[tid] and t.size <= 1048576 else 60
                                    delay = 0 if key[1] == -1 else 500 + max(1, math.ceil(t.size / 60))
                                    previous = release + communication * (delay + max(1, math.ceil(t.size / bandwidth)))
                                    local_routes[key] = previous
                                    transferred += t.size * (1 if key[1] == -1 else 2)
                                    new_reads.append(tid)
                                ready = max(ready, previous)
                        local_ends[o] = ready + max(1, ir.ops[o]['cycles'])
                        ptime[pipe] = local_ends[o]
                    end = max(ptime)
                    live = resident[core] | view['inputs'][b] | view['outputs'][b]
                    live_sizes = defaultdict(int)
                    for tid in live:
                        if counts[tid] > 0:
                            t = self.tensors[tid]
                            live_sizes['L1' if t.pos == 'L1' else 'UB'] += t.size
                    pressure = sum(max(0, live_sizes[p] - cap) for p, cap in [('L1', 524288), ('UB', 131072)]) / 60
                # Balance estimated two-pipe finishing time, traffic and lifetime.
                rank = (end + residency * pressure + .02 * communication * transferred / 60,
                        max(available[:core] + [end] + available[core + 1:]), transferred, core)
                options.append((rank, core, end, ptime, local_ends, local_routes, transferred, new_reads))
            _, core, end, ptime, local_ends, local_routes, transferred, new_reads = min(options, key=lambda x: x[0])
            for o in blocks[b]:
                assignment[o] = core
            finish.update(local_ends)
            routes.update(local_routes)
            pipes[core] = ptime
            available[core] = end
            bfinish[b] = end
            byte_total += transferred
            task_count[core] += 1
            for tid in new_reads:
                shared_reads[tid].add(core)
            resident[core].update(view['outputs'][b] | view['inputs'][b])
            for o in blocks[b]:
                for tid in self.inputs[o]:
                    counts[tid] -= 1
            for c in range(cores):
                resident[c].difference_update(t for t in tuple(resident[c]) if counts[t] <= 0)
        schedules = [[] for _ in range(cores)]
        for b in order:
            schedules[assignment[blocks[b][0]]].append(b)
        plan = {'node_to_subgraph': {str(o): view['owner'][o] for b in order for o in blocks[b]}, 'core_schedules': schedules}
        validate_plan(ir, plan)
        return plan, dict(proxy=max(max(available, default=0), byte_total / 60),
            proxy_copy_bytes=byte_total, region_count=len(blocks), core_pipe_ends=pipes,
            proxy_scope='ranking only; approximate residency and reusable reads, no official FIFO/spill timing')


def owner_map(plan):
    owner = {sg: c for c, seq in enumerate(plan['core_schedules']) for sg in seq}
    return {int(o): owner[t] for o, t in plan['node_to_subgraph'].items()}


def candidate_stream(structure, scene, cores, deadline=float('inf')):
    """Lazy routed families: new scene-specific candidates get early slots."""
    s = structure
    families = ['branch', 'affinity', 'wavefront']
    if s.profile['mean_cycles'] <= 100:
        families = ['affinity', 'wavefront', 'branch']
    scales = (2, 4, 1)
    for scale in scales:
        for family in families:
            blocks = s.regions(cores, scale, family, deadline)
            if scene == 1:
                specs = [('critical', 1., 0.)]
            elif scene == 2:
                specs = [('residency', 1., .5), ('critical', .4, 0.)]
            else:
                specs = [('cache_window', .6, .25), ('residency', 1., .5)]
            for ordering, communication, residency in specs:
                plan, meta = s.assign(blocks, scene, cores, ordering, communication, residency, deadline=deadline)
                yield dict(name=f'v2_p{scene}_{family}_s{scale}_{ordering}', plan=plan,
                    metadata=dict(family=f'p{scene}_{family}', scale=scale, ordering=ordering,
                        communication=communication, residency=residency, new_strategy=True, **meta))
