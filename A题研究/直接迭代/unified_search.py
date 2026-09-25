"""Complete-candidate scoring, diverse elites and cross-parent regional repair.

Analytical estimates and experience only rank candidates. Neither admits a plan
without an official evaluation. Models use structural features, never case IDs.
"""
from collections import defaultdict
import heapq
import math
import statistics
import time

from common_run import validate_plan
from unified_structure import owner_map


def analytical_features(s, plan, scene):
    ir = s.ir
    cores = len(plan['core_schedules'])
    assignment = owner_map(plan)
    groups = {int(o): t for o, t in plan['node_to_subgraph'].items()}
    work = [[0, 0] for _ in range(cores)]
    task_work = defaultdict(lambda: [0, 0])
    for o in ir.compute_ids:
        pipe = 0 if ir.ops[o]['pipe'] == 'PIPE_M' else 1
        work[assignment[o]][pipe] += max(1, ir.ops[o]['cycles'])
        task_work[groups[o]][pipe] += max(1, ir.ops[o]['cycles'])
    traffic, repeat, routes = 0, 0, 0
    task_bytes = defaultdict(int)
    for tid, t in s.tensors.items():
        own = groups if scene == 1 else assignment
        src, dst = {own[o] for o in t.producers}, {own[o] for o in t.consumers}
        if not src:
            reads = len(dst)
            for target in dst:
                task_bytes[target] += t.size
            writes = 0
        elif scene == 1:
            read_dst = {c for c in dst if any(p != c for p in src)}
            write_src = {p for p in src if t.output or any(c != p for c in dst)}
            reads, writes = len(read_dst), len(write_src)
            for task in read_dst | write_src:
                task_bytes[task] += t.size
        else:
            cross = sum(a != b for a in src for b in dst)
            routes += cross
            reads, writes = cross, cross + (len(src) if t.output else 0)
        traffic += (reads + writes) * t.size
        if t.size <= 1048576:
            repeat += max(0, reads - 1) * t.size
    peak_work = max(max(w) for w in work)
    path = max(s.tail.values(), default=0)
    necessary_time = max(peak_work, path)
    if scene == 1:
        pred = {t: set() for t in task_work}
        owners = {t: c for c, seq in enumerate(plan['core_schedules']) for t in seq}
        for o in ir.compute_ids:
            for p in ir.predecessors[o]:
                if groups[o] != groups[p]:
                    pred[groups[o]].add(groups[p])
        for seq in plan['core_schedules']:
            for a, b in zip(seq, seq[1:]):
                pred[b].add(a)
        succ = {t: set() for t in pred}
        for t, ps in pred.items():
            for p in ps:
                succ[p].add(t)
        degree = {t: len(ps) for t, ps in pred.items()}
        q = [t for t, d in degree.items() if not d]
        heapq.heapify(q)
        ends = {}
        compute_ends = {}
        while q:
            t = heapq.heappop(q)
            compute_ends[t] = max(task_work[t]) + max(
                (compute_ends[p] + (1000 if owners[p] != owners[t] else 100) for p in pred[t]), default=0)
            ends[t] = max(task_work[t]) + task_bytes[t] / 60 + max(
                (ends[p] + (1000 if owners[p] != owners[t] else 100) for p in pred[t]), default=0)
            for c in succ[t]:
                degree[c] -= 1
                if not degree[c]:
                    heapq.heappush(q, c)
        if len(ends) != len(pred):
            raise ValueError('candidate dependency cycle')
        path = max(ends.values(), default=0)
        necessary_time = max(necessary_time, max(compute_ends.values(), default=0))
    # P3's repeat bytes are potential, not a FIFO-hit prediction. A discounted
    # proxy is calibrated against measured complete plans by the ranker below.
    ddr = traffic - (.5 * repeat if scene == 3 else 0)
    proxy = max(peak_work, path, ddr / 60) + .05 * ddr / 60
    return dict(proxy=proxy, necessary_time=necessary_time, compute_peak=peak_work, compute_path=path, copy_bytes=traffic,
        repeat_bytes=repeat, cross_routes=routes, regions=len(task_work),
        operations=len(ir.compute_ids), dominant=s.profile['dominant_fraction'],
        minor_pipe=s.profile['minor_pipe_fraction'], scene=scene, cores=cores)


class Ranker:
    def __init__(self, experiences=()):
        self.rows = list(experiences)

    @staticmethod
    def vector(f):
        return [math.log1p(f['operations']), f['dominant'] * 3, f['minor_pipe'] * 3,
                math.log1p(f['copy_bytes'] / max(1, f['compute_peak'])),
                math.log1p(f['regions'] / max(1, f['cores']))]

    def observe(self, family, features, record):
        if record['status'] == 'success':
            self.rows.append(dict(family=family, features=features,
                ratio=record['metrics']['makespan'] / max(1, features['proxy']),
                seconds=record['elapsed_seconds'], success=True))
        else:
            self.rows.append(dict(family=family, features=features, ratio=None,
                                  seconds=record['elapsed_seconds'], success=False))

    def predict(self, family, f):
        vector = self.vector(f)
        rows = [r for r in self.rows if r['features']['scene'] == f['scene']]
        def distance(r):
            d = sum((a-b)**2 for a, b in zip(vector, self.vector(r['features'])))
            return d + (0 if r['family'] == family else 2)
        rows = sorted(rows, key=distance)[:7]
        good = [r for r in rows if r['success']]
        ratio = statistics.median(r['ratio'] for r in good) if good else 1.
        cost = statistics.median(r['seconds'] * f['operations'] / max(1, r['features']['operations']) for r in rows) if rows else 1.
        probability = (1 + len(good)) / (2 + len(rows))
        return f['proxy'] * ratio, max(.05, cost), probability

    def priority(self, c, incumbent_time):
        pred, seconds, probability = self.predict(c['metadata'].get('family', 'unknown'), c['features'])
        # Positive exploration floor keeps uncertain new families eligible.
        benefit = max(.01 / max(1, incumbent_time), 1 / max(1, pred) - 1 / max(1, incumbent_time))
        return probability * benefit / seconds


class ElitePool:
    def __init__(self, limit=6):
        self.limit = limit
        self.items = []

    def add(self, candidate, record):
        if record['status'] != 'success':
            return
        item = dict(candidate=candidate, record=record)
        owner = owner_map(candidate['plan'])
        # Ownership and partition membership both count toward diversity.
        near = []
        for i, old in enumerate(self.items):
            old_owner = owner_map(old['candidate']['plan'])
            distance = sum(owner[o] != old_owner[o] for o in owner) / max(1, len(owner))
            same_partition = candidate['plan']['node_to_subgraph'] == old['candidate']['plan']['node_to_subgraph']
            if distance < .03 and same_partition:
                near.append(i)
        if near:
            i = near[0]
            if record['metrics']['makespan'] < self.items[i]['record']['metrics']['makespan']:
                self.items[i] = item
        else:
            self.items.append(item)
        # Keep the fastest plus traffic-efficient alternatives, not only the
        # six smallest times from one structurally similar family.
        if len(self.items) > self.limit:
            self.items.sort(key=lambda x: x['record']['metrics']['makespan'])
            fastest = self.items[:self.limit-1]
            extra = min(self.items[self.limit-1:], key=lambda x: x['candidate'].get('features', {}).get('copy_bytes', float('inf')))
            self.items = fastest + [extra]


def joint_repairs(s, scene, cores, elites, deadline, generation=0):
    """Release whole parent Tasks around a heavy region; repair the complete DAG.

    Outside core ownership remains fixed. Inside may use another parent's
    allocation or a fresh multiscale allocation. All outputs undergo global
    legality and analytical scoring, including non-improving intermediate forms.
    """
    if not elites:
        return
    ordered = sorted(elites, key=lambda x: x['record']['metrics']['makespan'])
    parent = ordered[generation % len(ordered)]
    original = parent['candidate']['plan']
    by_task = defaultdict(list)
    for o in s.topo:
        by_task[original['node_to_subgraph'][str(o)]].append(o)
    task_work = {t: sum(s.ir.ops[o]['cycles'] for o in ops) for t, ops in by_task.items()}
    heavy = sorted(by_task, key=lambda t: (-task_work[t], t))
    selected = set(heavy[:max(1, min(4, len(heavy)//5))])
    selected_ops = {o for t in selected for o in by_task[t]}
    # Adjacent old Tasks join the destroyed region, enabling boundary changes.
    for o in tuple(selected_ops):
        for q in s.ir.predecessors[o] + s.ir.successors[o]:
            selected.add(original['node_to_subgraph'][str(q)])
    selected_ops = {o for t in selected for o in by_task[t]}
    old_owner = owner_map(original)
    other = ordered[(generation+1) % len(ordered)]
    other_owner = owner_map(other['candidate']['plan'])
    for family in ('branch', 'affinity'):
        if time.monotonic() >= deadline:
            return
        global_blocks = s.regions(cores, 4 if generation % 2 else 2, family, deadline)
        replacement = [[o for o in b if o in selected_ops] for b in global_blocks]
        replacement = [b for b in replacement if b]
        blocks = [ops for t, ops in by_task.items() if t not in selected] + replacement
        fixed_outside = {i: old_owner[b[0]] for i, b in enumerate(blocks) if b[0] not in selected_ops}
        for mode in ('reallocate', 'crossover'):
            fixed = dict(fixed_outside)
            if mode == 'crossover':
                for i, block in enumerate(blocks):
                    if i not in fixed:
                        totals = defaultdict(int)
                        for o in block:
                            totals[other_owner[o]] += max(1, s.ir.ops[o]['cycles'])
                        fixed[i] = max(totals, key=lambda c: (totals[c], -c))
            try:
                plan, meta = s.assign(blocks, scene, cores,
                    'cache_window' if scene == 3 else 'residency', communication=1.,
                    residency=.5 if scene > 1 else 0., fixed=fixed, deadline=deadline)
            except ValueError:
                continue
            base = dict(family=f'joint_{mode}_{family}', new_strategy=True,
                parent=parent['candidate']['name'], second_parent=other['candidate']['name'],
                destroyed_tasks=sorted(selected), destroyed_ops=len(selected_ops), **meta)
            yield dict(name=f'joint_g{generation}_{mode}_{family}', plan=plan, metadata=base)
            if scene == 1:
                from p1_bottleneck_repartition import merge_region
                protected = set(s.ir.compute_ids) - selected_ops
                target = max(1000, sum(task_work[t] for t in selected) / max(1, cores))
                merged = merge_region(s.ir, plan, protected, target, time.perf_counter()+max(0, deadline-time.monotonic()))
                validate_plan(s.ir, merged)
                yield dict(name=f'joint_g{generation}_{mode}_{family}_merge', plan=merged,
                           metadata={**base, 'family': base['family']+'_merge', 'merged_complete_candidate': True})


class Search:
    """Bounded lookahead, on-line calibration and complete regional proposals."""
    def __init__(self, structure, scene, cores, old, new, experiences=()):
        self.s, self.scene, self.cores = structure, scene, cores
        self.streams = {'old': old, 'new': new}
        self.exhausted = set()
        self.queue = []
        self.seen = set()
        self.ranker = Ranker(experiences)
        self.elites = ElitePool(6)
        self.errors = []
        self.repair_generations = 0
        self.last_repair_call = -1
        self.pruned_by_bound = 0
        self.prune_rounds = 0

    def admit(self, candidate, source):
        from unified_solver import signature
        identity = signature(candidate['plan'])
        if identity in self.seen:
            return False
        self.seen.add(identity)
        validate_plan(self.s.ir, candidate['plan'])
        candidate['features'] = analytical_features(self.s, candidate['plan'], self.scene)
        candidate['metadata'] = {**candidate['metadata'], 'analytical': candidate['features']}
        self.queue.append((candidate, source))
        return True

    def fill(self, source, count, deadline):
        attempted = 0
        while source not in self.exhausted and count > 0 and time.monotonic() < deadline and attempted < 30:
            attempted += 1
            try:
                c = next(self.streams[source])
                count -= self.admit(c, source)
            except StopIteration:
                self.exhausted.add(source)
            except (ValueError, TimeoutError) as error:
                self.errors.append(dict(source=source, error=str(error)))
                self.exhausted.add(source)

    def next(self, calls, best, deadline):
        if not calls:
            self.fill('old', 1, deadline)
            return self.queue.pop(0) if self.queue else (None, 'old')
        if len(calls) == 1:
            self.fill('new', 1, deadline)
            choices = [(i, c) for i, (c, source) in enumerate(self.queue) if source == 'new']
            if choices:
                return self.queue.pop(choices[0][0])
        if len(calls) == 2 and self.scene in (2, 3):
            # Preserve the second distinct mature assignment before calibrated
            # search; sparse observations can mis-rank nearby communication
            # weights even though both seeds are cheap and complementary.
            self.fill('old', 1, deadline)
            choices = [i for i, (_, source) in enumerate(self.queue) if source == 'old']
            if choices:
                return self.queue.pop(choices[0])
        # Replenish mature and new families together. Avoid spending the entire
        # generation budget looking through a large duplicate sequence.
        generation_deadline = min(deadline, time.monotonic()+12)
        self.fill('old', max(0, 4-sum(source=='old' for _, source in self.queue)), generation_deadline)
        self.fill('new', max(0, 3-sum(source=='new' for _, source in self.queue)), generation_deadline)
        if best and len(calls) >= 4 and len(calls)-self.last_repair_call >= 4:
            self.last_repair_call = len(calls)
            try:
                for c in joint_repairs(self.s, self.scene, self.cores, self.elites.items,
                                       min(deadline, time.monotonic()+6), self.repair_generations):
                    self.admit(c, 'joint')
                    if sum(source=='joint' for _, source in self.queue) >= 4:
                        break
            except (ValueError, TimeoutError) as error:
                self.errors.append(dict(source='joint', error=str(error)))
            self.repair_generations += 1
            if self.scene == 1 and time.monotonic() < deadline:
                import gzip, json
                from common_run import read_json
                from p1_task_refine import generate
                from p1_portfolio import refinement_caps
                rec = best['record']
                plan = read_json(rec['plan_path'])
                caps = refinement_caps(plan, self.cores)
                if caps:
                    with gzip.open(rec['result_path'], 'rt') as handle:
                        raw = json.load(handle)
                    for c in generate(self.s.ir, plan, raw, 17, merge_caps=caps[:2]):
                        c['name'] = f"joint_task_g{self.repair_generations}_" + c['name']
                        c['metadata'] = {**c['metadata'], 'family': 'joint_task_merge', 'new_strategy': True}
                        self.admit(c, 'joint')
            # Observed FIFO timing refines a parent that may itself have a new
            # core assignment; this is no longer a fixed initial mapping route.
            if self.scene == 3 and time.monotonic() < deadline:
                import gzip, json
                from p3_read_order import generate
                from common_run import read_json
                rec = best['record']
                with gzip.open(rec['result_path'], 'rt') as handle:
                    raw = json.load(handle)
                try:
                    cache, _ = generate(self.s.ir, read_json(rec['plan_path']), raw, self.cores, limit=4)
                    for c in cache[1:]:
                        c['name'] = f"joint_fifo_g{self.repair_generations}_" + c['name']
                        c['metadata'] = {**c['metadata'], 'family': 'joint_observed_fifo', 'new_strategy': True}
                        self.admit(c, 'joint')
                except ValueError as error:
                    self.errors.append(dict(source='observed_fifo', error=str(error)))
        if not self.queue:
            return None, 'exhausted'
        incumbent_time = best['record']['metrics']['makespan'] if best else float('inf')
        retained = [(c, source) for c, source in self.queue if c['features']['necessary_time'] <= incumbent_time]
        self.pruned_by_bound += len(self.queue)-len(retained)
        self.queue = retained
        if not self.queue:
            self.prune_rounds += 1
            if self.prune_rounds < 3 and len(self.exhausted) < 2 and time.monotonic() < deadline:
                return self.next(calls, best, deadline)
            return None, 'necessary_bounds'
        candidates = list(range(len(self.queue)))
        if len(calls) == 2:
            candidates = [i for i in candidates if self.queue[i][1] == 'old'] or candidates
        # Ensure at least one full regional action is actually evaluated, then
        # let measured calibration decide further spending.
        if len(calls) >= 4 and not any(x['source']=='joint' for x in calls):
            candidates = [i for i in candidates if self.queue[i][1]=='joint'] or candidates
        chosen = max(candidates, key=lambda i: self.ranker.priority(self.queue[i][0], incumbent_time))
        return self.queue.pop(chosen)

    def observe(self, candidate, record):
        features = candidate.get('features') or analytical_features(self.s, candidate['plan'], self.scene)
        if record['status'] == 'success' and features['necessary_time'] > record['metrics']['makespan']:
            raise AssertionError('necessary bound exceeds official time')
        self.prune_rounds = 0
        self.ranker.observe(candidate['metadata'].get('family', 'unknown'), features, record)
        self.elites.add({**candidate, 'features': features}, record)
