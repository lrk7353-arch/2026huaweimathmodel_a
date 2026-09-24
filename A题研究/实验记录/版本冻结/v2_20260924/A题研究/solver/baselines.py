"""Bounded, deterministic WCC packing baselines; no evaluator imports/calls.

Affinity always includes every simple candidate. Proxy scores are heuristic
screening features, never official makespans or feasibility certificates.
"""
import random

if __package__:
    from .plan import _positive_cores, component_groups, plan_from_component_groups
else:
    from plan import _positive_cores, component_groups, plan_from_component_groups


def _load_order(ir):
    return sorted(range(len(ir.components)), key=lambda i: (
        -max(ir.components[i].work_m, ir.components[i].work_v, ir.components[i].work_other),
        -ir.components[i].compute_work, ir.components[i].nodes[0]))


def _score(load_m, load_v, reads, traffic_weight):
    peak = max(load_m + load_v, default=0)
    ddr = reads / 60.0
    return max(peak, ddr) + traffic_weight * ddr


def _greedy(ir, active, order, traffic_weight=None):
    assignments = [-1] * len(ir.components)
    lm, lv = [0] * active, [0] * active
    inputs = [set() for _ in range(active)]
    reads = 0
    for position, cid in enumerate(order):
        component = ir.components[cid]
        candidates = [position] if position < active else range(active)
        def key(core):
            m, v = lm[core] + component.work_m, lv[core] + component.work_v
            new_bytes = sum(ir.input_sizes[t] for t in component.input_ids if t not in inputs[core])
            if traffic_weight is None:
                return (max(m, v), m + v, core)
            proposed_m, proposed_v = list(lm), list(lv)
            proposed_m[core], proposed_v[core] = m, v
            return (_score(proposed_m, proposed_v, reads + new_bytes, traffic_weight),
                    max(m, v), new_bytes, core)
        core = min(candidates, key=key)
        assignments[cid] = core
        lm[core] += component.work_m
        lv[core] += component.work_v
        reads += sum(ir.input_sizes[t] for t in component.input_ids if t not in inputs[core])
        inputs[core].update(component.input_ids)
    return assignments


class _State:
    def __init__(self, ir, assignment, active, weight):
        self.ir, self.assignment, self.active, self.weight = ir, list(assignment), active, weight
        self.m, self.v = [0] * active, [0] * active
        self.count = [0] * active
        self.refs = [{} for _ in range(active)]
        self.reads = 0
        for cid, core in enumerate(assignment):
            self._add(cid, core, 1)

    def _add(self, cid, core, sign):
        c = self.ir.components[cid]
        self.m[core] += sign * c.work_m
        self.v[core] += sign * c.work_v
        self.count[core] += sign
        refs = self.refs[core]
        for tid in c.input_ids:
            old = refs.get(tid, 0)
            new = old + sign
            if new < 0:
                raise AssertionError("negative input reference count")
            if old == 0:
                self.reads += self.ir.input_sizes[tid]
            if new == 0:
                self.reads -= self.ir.input_sizes[tid]
                refs.pop(tid, None)
            else:
                refs[tid] = new

    def score(self):
        return _score(self.m, self.v, self.reads, self.weight)

    def move(self, cid, target):
        source = self.assignment[cid]
        self._add(cid, source, -1)
        self._add(cid, target, 1)
        self.assignment[cid] = target

    def swap(self, a, b):
        ca, cb = self.assignment[a], self.assignment[b]
        self.move(a, cb)
        self.move(b, ca)


def _improve(ir, assignment, active, weight, rng):
    """At most 2 passes over 64 components + 96 swaps; no wall-time randomness."""
    state = _State(ir, assignment, active, weight)
    initial = state.score()
    if active <= 1:
        return list(assignment), {"initial_proxy": initial, "final_proxy": initial,
                                  "accepted_moves": 0, "accepted_swaps": 0,
                                  "move_trials": 0, "swap_trials": 0}
    ranked = _load_order(ir)
    if len(ranked) > 64:
        heavy_input = sorted(ranked, key=lambda i: (-ir.components[i].input_bytes, i))
        selected = list(dict.fromkeys(ranked[:24] + heavy_input[:24]))
        remaining = [i for i in ranked if i not in set(selected)]
        selected += rng.sample(remaining, min(64 - len(selected), len(remaining)))
    else:
        selected = list(ranked)
    moves = swaps = move_trials = swap_trials = 0
    for _ in range(2):
        changed = False
        for cid in selected:
            source = state.assignment[cid]
            if state.count[source] <= 1:
                continue
            best = state.score()
            destination = source
            for target in range(active):
                if target == source:
                    continue
                state.move(cid, target)
                score = state.score()
                state.move(cid, source)
                move_trials += 1
                if score < best - 1e-9:
                    best, destination = score, target
            if destination != source:
                state.move(cid, destination)
                moves += 1
                changed = True
        if len(selected) > 1:
            pairs = [(a, b) for i, a in enumerate(selected) for b in selected[i + 1:]]
            if len(pairs) > 96:
                pairs = rng.sample(pairs, 96)
            for a, b in pairs:
                if state.assignment[a] == state.assignment[b]:
                    continue
                before = state.score()
                state.swap(a, b)
                swap_trials += 1
                if state.score() < before - 1e-9:
                    swaps += 1
                    changed = True
                else:
                    state.swap(a, b)
        if not changed:
            break
    return state.assignment, {"initial_proxy": initial, "final_proxy": state.score(),
                              "accepted_moves": moves, "accepted_swaps": swaps,
                              "move_trials": move_trials, "swap_trials": swap_trials}


def generate_candidates(ir, num_cores, method="simple", seed=0):
    """Return [{name, plan, metadata}], covering <=N active cores and 2 granularities.

    WCCs stay whole. Duplicate partitions/orders are removed before large JSON
    creation; metadata['also_generated_as'] records equivalent origins.
    Affinity uses four fixed starts per active count and emits at most two
    affinity assignments in addition to simple incumbents. No model timings
    are claimed by metadata proxy values.
    """
    _positive_cores(num_cores)
    if method not in ("simple", "affinity"):
        raise ValueError("method must be simple or affinity")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    result, seen = [], {}
    load_order = _load_order(ir)

    def emit(assignment, order, active, source, variant, detail=None):
        for granularity in ("per_core", "per_component"):
            groups = component_groups(ir, assignment, num_cores, granularity, order)
            alias = {"source": source, "active_cores": active, "granularity": granularity,
                     "variant": variant}
            if groups in seen:
                seen[groups]["metadata"]["also_generated_as"].append(alias)
                continue
            state = _State(ir, assignment, max(1, active), 0.0)
            metadata = {"source": source, "method": source, "seed": seed,
                        "active_cores": active, "num_cores": num_cores,
                        "granularity": granularity, "variant": variant,
                        "component_count": len(ir.components), "splits_components": False,
                        "proxy_core_work_m": list(state.m), "proxy_core_work_v": list(state.v),
                        "proxy_total_input_read_bytes": state.reads,
                        "proxy_scope": "whole-component per-core packing; excludes spill, sync, FIFO, output traffic",
                        "also_generated_as": []}
            if detail:
                metadata.update(detail)
            record = {"name": "{}_a{}_{}_{}".format(source, active, granularity, variant),
                      "plan": plan_from_component_groups(ir, groups), "metadata": metadata}
            seen[groups] = record
            result.append(record)

    if not ir.components:
        emit([], [], 0, "simple", "empty")
        return result
    # Empty trailing cores preserve smaller-active-core incumbents in N-core plans.
    active_counts = range(1, min(num_cores, len(ir.components)) + 1)
    for active in active_counts:
        assignment = _greedy(ir, active, load_order)
        emit(assignment, load_order, active, "simple", "lpt_mv")
    if method == "simple":
        return result
    for active in active_counts:
        if active == 1:
            continue
        for weight in (0.15, 0.75):
            choices = []
            for start in range(2):
                rng = random.Random(seed + 1009 * active + 9176 * start + int(weight * 100))
                if start == 0:
                    order = list(load_order)
                else:
                    jitter = {i: 0.8 + 0.4 * rng.random() for i in load_order}
                    order = sorted(load_order, key=lambda i: (
                        -max(ir.components[i].work_m, ir.components[i].work_v) * jitter[i], i))
                initial = _greedy(ir, active, order, weight)
                improved, stats = _improve(ir, initial, active, weight, rng)
                choices.append((stats["final_proxy"], start, improved, order, stats))
            _, start, assignment, order, stats = min(choices, key=lambda x: (x[0], x[1]))
            detail = dict(stats, traffic_weight=weight, selected_start=start, starts_tested=2,
                          proxy_bandwidth_bytes_per_cycle=60)
            emit(assignment, order, active, "affinity", "w{:02d}".format(int(weight * 100)), detail)
    return result
