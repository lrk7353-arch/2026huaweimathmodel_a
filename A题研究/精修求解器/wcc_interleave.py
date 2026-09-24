"""Pure fixed-core WCC phase interleaving for P2/P3; never runs an evaluator.

The scheduler below predicts only local compute availability. Its tensor-lifetime
estimate excludes COPY prefetch, Step2 spill, Step3 memory reuse, DDR contention
and L2. Neither score nor active-WCC window certifies capacity or true runtime.
"""
from collections import Counter, defaultdict, deque
import hashlib
import heapq
import json
from pathlib import Path
import sys

try:
    from solver.plan import validate_plan
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))
    from plan import validate_plan

PIPES = ("PIPE_M", "PIPE_V")


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _assignment(ir, plan, cores):
    validate_plan(ir, plan)
    if len(plan["core_schedules"]) != cores:
        raise ValueError("num_cores must equal schedule count, including empty cores")
    by_sg = {sg: c for c, seq in enumerate(plan["core_schedules"]) for sg in seq}
    return {int(op): by_sg[sg] for op, sg in plan["node_to_subgraph"].items()}


def _is_topology(ir, order):
    if len(order) != len(ir.compute_ids) or set(order) != set(ir.compute_ids):
        return False
    rank = {o: i for i, o in enumerate(order)}
    return all(rank[o] < rank[n] for o in order for n in ir.successors[o])


def _control_order(ir, plan, assignment):
    """Recover expressed priorities, not the official Step1 execution sequence."""
    mapping = {int(o): sg for o, sg in plan["node_to_subgraph"].items()}
    sg_rank = {sg: i for seq in plan["core_schedules"] for i, sg in enumerate(seq)}
    order = list(mapping)
    last = defaultdict(lambda: -1)
    monotone = True
    for o in order:
        c, rank = assignment[o], sg_rank[mapping[o]]
        monotone &= rank >= last[c]
        last[c] = rank
    if monotone and _is_topology(ir, order):
        return order, "input_mapping_topology_respecting_core_sg_order"
    priority = {o: (assignment[o], sg_rank[mapping[o]], i, o)
                for i, o in enumerate(order)}
    degree = {o: len(ir.predecessors[o]) for o in ir.compute_ids}
    ready = [(priority[o], o) for o in ir.compute_ids if degree[o] == 0]
    heapq.heapify(ready)
    result = []
    while ready:
        _, o = heapq.heappop(ready)
        result.append(o)
        for n in ir.successors[o]:
            degree[n] -= 1
            if degree[n] == 0:
                heapq.heappush(ready, (priority[n], n))
    if not _is_topology(ir, result):
        raise ValueError("cannot recover legal compute topology")
    return result, "topological_recovery_with_core_sg_and_mapping_priority"


def _encode(ir, order, assignment, cores):
    if not _is_topology(ir, order):
        raise ValueError("encoding requires exact global compute topology")
    mapping, schedules = {}, [[] for _ in range(cores)]
    for sg, o in enumerate(order):
        mapping[str(o)] = sg
        schedules[assignment[o]].append(sg)
    plan = {"node_to_subgraph": mapping, "core_schedules": schedules}
    validate_plan(ir, plan)
    return plan


def _memory_views(ir):
    incident = {o: set() for o in ir.compute_ids}
    for e in ir.graph["edges"]:
        a, b = e["source"], e["target"]
        if a in incident and b in ir.tensors and ir.tensors[b]["pos"] != "DDR":
            incident[a].add(b)
        if b in incident and a in ir.tensors and ir.tensors[a]["pos"] != "DDR":
            incident[b].add(a)
    return incident


def _component_order(ir, cids, policy, base_rank, seed):
    if policy == "base":
        return sorted(cids, key=lambda c: (base_rank[c], c))
    # Three dominance queues keep complementary admission O(C log C), avoiding
    # quadratic scans of all remaining WCCs. Tie noise is seed-stable, not IDs.
    queues = defaultdict(list)
    for c in cids:
        w = ir.components[c]
        bucket = (w.work_m > w.work_v) - (w.work_m < w.work_v)
        noise = hashlib.sha256(f"{seed}:{c}".encode()).hexdigest()
        queues[bucket].append((-max(w.work_m, w.work_v), noise, c))
    for queue in queues.values():
        heapq.heapify(queue)
    result, balance = [], 0
    while any(queues.values()):
        heads = [queue[0][2] for queue in queues.values() if queue]
        c = min(heads, key=lambda cid: (
            abs(balance + ir.components[cid].work_m - ir.components[cid].work_v),
            -max(ir.components[cid].work_m, ir.components[cid].work_v),
            hashlib.sha256(f"{seed}:{cid}".encode()).hexdigest()))
        w = ir.components[c]
        heapq.heappop(queues[(w.work_m > w.work_v) - (w.work_m < w.work_v)])
        result.append(c)
        balance += w.work_m - w.work_v
    return result


def _local_order(ir, cids, window, admission, dispatch, base_rank, tail,
                 incident, seed):
    pending = deque(_component_order(ir, cids, admission, base_rank, seed))
    members = {c: ir.components[c].nodes for c in cids}
    degree = {o: len(ir.predecessors[o]) for c in cids for o in members[c]}
    left = {c: len(members[c]) for c in cids}
    ready = {(c, p): [] for c in cids for p in PIPES}
    active, peak_active = set(), 0
    def fill():
        nonlocal peak_active
        while pending and len(active) < window:
            c = pending.popleft()
            active.add(c)
            for o in members[c]:
                if degree[o] == 0:
                    heapq.heappush(ready[c, ir.ops[o]["pipe"]], (-tail[o], o))
        peak_active = max(peak_active, len(active))
    fill()
    pipe_end = {p: 0 for p in PIPES}
    finish, order = {}, []
    remaining = Counter(t for o in degree for t in incident[o])
    live, used = set(), {"L1": 0, "UB": 0}
    peak = dict(used)
    while active:
        choices = []
        for c in sorted(active):
            for pipe in PIPES:
                if ready[c, pipe]:
                    _, o = ready[c, pipe][0]
                    start = max(pipe_end[pipe], max((finish[p] for p in ir.predecessors[o]), default=0))
                    end = start + max(1, ir.ops[o]["cycles"])
                    allocated = sum(ir.tensors[t]["size"] for t in incident[o] if t not in live)
                    freed = sum(ir.tensors[t]["size"] for t in incident[o] if remaining[t] == 1)
                    key = ((start, allocated - freed, end, -tail[o], base_rank[c], o)
                           if dispatch == "memory_tie" else
                           (start, end, -tail[o], base_rank[c], o))
                    choices.append((key, o, c, pipe, end))
        if not choices:
            raise ValueError("active WCC has no ready operation")
        _, o, c, pipe, end = min(choices)
        heapq.heappop(ready[c, pipe])
        order.append(o)
        finish[o], pipe_end[pipe] = end, end
        for t in incident[o]:
            if t not in live:
                live.add(t)
                used[ir.tensors[t]["pos"]] += ir.tensors[t]["size"]
        for pos in used:
            peak[pos] = max(peak[pos], used[pos])
        for t in incident[o]:
            remaining[t] -= 1
            if remaining[t] == 0:
                live.remove(t)
                used[ir.tensors[t]["pos"]] -= ir.tensors[t]["size"]
        for n in ir.successors[o]:
            degree[n] -= 1
            if degree[n] == 0:
                heapq.heappush(ready[c, ir.ops[n]["pipe"]], (-tail[n], n))
        left[c] -= 1
        if left[c] == 0:
            active.remove(c)
            fill()
    if len(order) != len(degree):
        raise ValueError("local order misses compute operations")
    return order, {"peak_admitted_wccs": peak_active,
        "compute_only_predicted_end": max(pipe_end.values(), default=0),
        "compute_touch_live_bytes_proxy": peak,
        "compute_work": {p: sum(max(1, ir.ops[o]["cycles"]) for o in order
                                  if ir.ops[o]["pipe"] == p) for p in PIPES}}


def generate_interleave_candidates(ir, plan, *, num_cores, max_candidates=12, seed=17):
    """Return at most one reencoding control + eight fixed-core window plans.

    Split WCCs are explicitly unsupported. Each emitted plan preserves every
    original compute op's core, not necessarily Step1's original internal order.
    Single-op SGs express a legal global topology. Intended for P2/P3 only;
    evaluating these as P1 would impose one Task per operation.
    """
    if type(num_cores) is not int or not 1 <= num_cores <= 5:
        raise ValueError("num_cores must be an integer in 1..5")
    if type(max_candidates) is not int or max_candidates < 0:
        raise ValueError("max_candidates must be a nonnegative integer")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    assignment = _assignment(ir, plan, num_cores)
    core_components = [[] for _ in range(num_cores)]
    split = []
    for c in ir.components:
        cores = {assignment[o] for o in c.nodes}
        if len(cores) != 1:
            split.append(c.id)
        else:
            core_components[next(iter(cores))].append(c.id)
    diag = {"schema_version": 1, "official_calls": 0, "seed": seed,
        "input_plan_sha256": _digest(plan), "max_candidates": max_candidates,
        "split_wcc_ids": split, "wcc_count_by_core": [len(x) for x in core_components],
        "applicable": False, "generated_count": 0, "discarded": [],
        "scope": "P2/P3 fixed-core WCC phase priority; no exact capacity or critical-path claim",
        "limitations": ["single-op reencoding may change official priority even in control",
            "compute-only pipe prediction omits COPY, memory reuse, spill, bandwidth, L2",
            "window limits admitted WCC count, not actual resident bytes",
            "lifetime proxy ignores COPY prefetch and post-compute final output lifetime",
            "official evaluation is mandatory for acceptance"]}
    if split:
        diag["reason"] = "input contains cross-core WCCs; no partial rewrite attempted"
        return [], diag
    if any(ir.ops[o]["pipe"] not in PIPES for o in ir.compute_ids):
        diag["reason"] = "non-COPY compute uses a pipe outside M/V"
        return [], diag
    eligible = [k for k, cids in enumerate(core_components) if len(cids) >= 2
                and sum(ir.components[c].work_m for c in cids) > 0
                and sum(ir.components[c].work_v for c in cids) > 0]
    if not eligible:
        diag["reason"] = "no core has multiple complete WCCs with both M and V work"
        return [], diag
    diag.update(applicable=True, eligible_cores=eligible)
    if max_candidates == 0:
        return [], diag
    base_order, origin = _control_order(ir, plan, assignment)
    diag["control_order_source"] = origin
    diag["control_claims_exact_original_step1_sequence"] = False
    position = {o: i for i, o in enumerate(base_order)}
    first = {c.id: min(position[o] for o in c.nodes) for c in ir.components}
    tail = {}
    for o in reversed(base_order):
        tail[o] = max(1, ir.ops[o]["cycles"]) + max((tail[n] for n in ir.successors[o]), default=0)
    incident = _memory_views(ir)
    candidates, seen = [], set()
    def emit(name, order, metadata):
        encoded = _encode(ir, order, assignment, num_cores)
        h = _digest(encoded)
        if h in seen:
            diag["discarded"].append({"name": name, "reason": "exact_duplicate_plan"})
            return
        if _assignment(ir, encoded, num_cores) != assignment:
            raise AssertionError("fixed-core invariant violated")
        seen.add(h)
        candidates.append({"name": name, "plan": encoded, "metadata": {
            "family": "wcc_interleave", "plan_sha256": h, "seed": seed,
            "assignment_unchanged": True, "original_op_count": len(ir.compute_ids),
            "subgraph_count": len(ir.compute_ids), "global_topology_sha256": _digest(order),
            "capacity_certified": False, "official_evaluation_required": True, **metadata}})
    emit("interleave_reencode_control", base_order, {"strategy": "reencode_control",
        "order_source": origin, "window": None, "original_step1_order_preserved_claimed": False})
    specs = [(w, a, "earliest_start") for w in (2, 4, 8) for a in ("base", "complement")]
    specs += [(w, "complement", "memory_tie") for w in (2, 4)]
    for window, admission, dispatch in specs:
        if len(candidates) >= max_candidates:
            break
        whole_order, per_core = [], []
        for core, cids in enumerate(core_components):
            local, description = _local_order(ir, cids, window, admission, dispatch,
                                               first, tail, incident, seed)
            whole_order.extend(local)
            per_core.append({"core": core, **description})
        emit(f"interleave_w{window}_{admission}_{dispatch}", whole_order,
             {"strategy": "window_interleave", "window": window,
              "admission": admission, "dispatch": dispatch, "per_core_proxy": per_core})
    diag["generated_count"] = len(candidates)
    return candidates, diag
