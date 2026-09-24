"""Pure, bounded P2/P3 trace-driven neighbourhood generation (stdlib).

The caller owns official evaluation, provenance checks, acceptance and iteration.
Late transfers / observed waits are ranking features, NOT exact critical paths.
Original dependency tails omit compiled memory-reuse edges, dynamic contention
and L2 state. Every emitted plan must still pass the official evaluator.
"""
from collections import Counter, defaultdict, deque
import hashlib
import heapq
import json
import math
from pathlib import Path
import sys

try:
    from solver.plan import validate_plan
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))
    from plan import validate_plan


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _topology(ir, priorities=None):
    priorities = priorities or {}
    degree = {op: len(ir.predecessors[op]) for op in ir.compute_ids}
    ready = [(priorities.get(op, (op,)), op) for op in ir.compute_ids if not degree[op]]
    heapq.heapify(ready)
    result = []
    while ready:
        _, op = heapq.heappop(ready)
        result.append(op)
        for nxt in ir.successors[op]:
            degree[nxt] -= 1
            if not degree[nxt]:
                heapq.heappush(ready, (priorities.get(nxt, (nxt,)), nxt))
    if len(result) != len(ir.compute_ids):
        raise ValueError("compute dependencies are cyclic")
    return result


def _is_topology(ir, order):
    if len(order) != len(ir.compute_ids) or set(order) != set(ir.compute_ids):
        return False
    positions = {op: i for i, op in enumerate(order)}
    return all(positions[op] < positions[nxt] for op in order for nxt in ir.successors[op])


def _plan_assignment(ir, plan, num_cores):
    validate_plan(ir, plan)
    if len(plan["core_schedules"]) != num_cores:
        raise ValueError("num_cores must match plan core_schedules, including empty cores")
    core_by_sg = {sg: core for core, sgs in enumerate(plan["core_schedules"]) for sg in sgs}
    return {int(op): core_by_sg[sg] for op, sg in plan["node_to_subgraph"].items()}


def _recover_order(ir, plan, assignment):
    """Dictionary order is usable only if both dependencies and SG order agree."""
    integer_mapping = {int(op): sg for op, sg in plan["node_to_subgraph"].items()}
    order = list(integer_mapping)
    ranks = {sg: i for schedule in plan["core_schedules"] for i, sg in enumerate(schedule)}
    last = defaultdict(lambda: -1)
    core_order_ok = True
    for op in order:
        rank = ranks[integer_mapping[op]]
        if rank < last[assignment[op]]:
            core_order_ok = False
        last[assignment[op]] = rank
    if _is_topology(ir, order) and core_order_ok:
        return order, "plan_dictionary_topology", False
    # A valid plan need not serialize its mapping topologically. Use a safe IR
    # topology and report this priority/control change rather than pretend the
    # original schedule has been exactly recovered.
    return _topology(ir), "ir_stable_topology_fallback", True


def _runs_plan(ir, order, assignment, num_cores, max_run_ops=None, boundary_by_op=None):
    if not _is_topology(ir, order):
        raise ValueError("run encoding requires a global compute topology")
    mapping, schedules = {}, [[] for _ in range(num_cores)]
    previous, previous_boundary, sg, count = None, None, -1, 0
    for op in order:
        core = assignment[op]
        if not 0 <= core < num_cores:
            raise ValueError("invalid assigned core")
        boundary = boundary_by_op[op] if boundary_by_op is not None else None
        if (core != previous or (boundary_by_op is not None and boundary != previous_boundary)
                or (max_run_ops is not None and count >= max_run_ops)):
            sg += 1
            schedules[core].append(sg)
            previous, count = core, 0
        previous_boundary = boundary
        mapping[str(op)] = sg
        count += 1
    result = {"node_to_subgraph": mapping, "core_schedules": schedules}
    validate_plan(ir, result)
    return result


def _tensor_views(ir):
    compute = set(ir.compute_ids)
    producers, consumers, incident = defaultdict(set), defaultdict(set), defaultdict(set)
    for edge in ir.graph["edges"]:
        a, b = edge["source"], edge["target"]
        if a in ir.ops and b in ir.tensors:
            if a in compute:
                producers[b].add(a)
                incident[a].add(b)
        elif a in ir.tensors and b in ir.ops:
            if b in compute:
                consumers[a].add(b)
                incident[b].add(a)
        else:
            raise ValueError("original graph is not Op/Tensor bipartite")
    return producers, consumers, incident


def _routes_for(tid, assignment, views):
    producers, consumers, _ = views
    return {(s, t) for s in {assignment[o] for o in producers[tid]}
            for t in {assignment[o] for o in consumers[tid]} if s != t}


def _traffic_delta(ir, assignment, changes, views):
    """Exact direct-original-tensor route byte delta; not total DDR traffic."""
    changed = dict(assignment)
    changed.update(changes)
    incident = set().union(*(views[2][op] for op in changes)) if changes else set()
    delta = sum(ir.tensors[t]["size"] * (len(_routes_for(t, changed, views)) -
                                           len(_routes_for(t, assignment, views))) for t in incident)
    return delta


def _trace(ir, assignment, official, num_cores, views, plan):
    if not isinstance(official, dict) or official.get("scene") != "B" or official.get("problem", 2) not in (2, 3):
        raise ValueError("official_result must be an unwrapped P2/P3 result (both use scene B)")
    if type(official.get("num_cores")) is not int or official["num_cores"] != num_cores:
        raise ValueError("official result/core count mismatch")
    if official.get("cross_core_copy_delay_cycles") != 500:
        raise ValueError("trace does not use the official 500-cycle cross-core release")
    makespan = official.get("makespan")
    if type(makespan) not in (int, float) or not math.isfinite(makespan) or makespan < 0:
        raise ValueError("invalid official makespan")
    expected_subgraphs = {int(op): sg for op, sg in plan["node_to_subgraph"].items()}
    timeline, original = {}, {}
    seen_cores = set()
    for core in official.get("per_core_timeline", []):
        c = core["core_id"]
        if type(c) is not int or not 0 <= c < num_cores or c in seen_cores:
            raise ValueError("invalid timeline core")
        seen_cores.add(c)
        observed_schedule = [sg for task in core.get("tasks", []) for sg in task.get("subgraph_ids", [])]
        if observed_schedule != plan["core_schedules"][c]:
            raise ValueError("trace subgraph order disagrees with exact plan")
        for op in core["ops"]:
            key = (c, op["op_id"])
            if (any(type(op[k]) not in (int, float) or not math.isfinite(op[k]) or op[k] < 0 for k in ("start", "end"))
                    or key in timeline or op["end"] < op["start"] or op["end"] > makespan):
                raise ValueError("duplicate or invalid timeline operation")
            timeline[key] = op
            if op["op_id"] in assignment:
                oid = op["op_id"]
                if (oid in original or assignment[oid] != c or op["op"] != ir.ops[oid]["op"]
                        or op.get("subgraph_id") != expected_subgraphs[oid]):
                    raise ValueError("trace original compute ids/core assignment disagree with plan")
                original[oid] = op
    if seen_cores != set(range(num_cores)) or set(original) != set(ir.compute_ids):
        raise ValueError("trace must cover every original compute operation exactly once")
    producers, consumers, _ = views
    routes, seen, skipped = [], set(), []
    raw = official.get("cross_core_transfers")
    if not isinstance(raw, list):
        raise ValueError("missing official cross_core_transfers list")
    for link in raw:
        tid, src, dst = link["tensor_id"], link["source_core"], link["target_core"]
        key = tid, src, dst
        if key in seen:
            raise ValueError("duplicate saved transfer route")
        seen.add(key)
        if tid not in ir.tensors:
            skipped.append({"route": key, "reason": "no original tensor id; attribution not guessed"})
            continue
        if link["size"] != ir.tensors[tid]["size"] or (src, dst) not in _routes_for(tid, assignment, views):
            raise ValueError("trace tensor size or core route disagrees with original graph/plan")
        try:
            outgoing = timeline[src, link["source_copy_out_id"]]
            incoming = timeline[dst, link["target_copy_in_id"]]
        except KeyError as error:
            raise ValueError("COPY operation absent from timeline") from error
        if not (outgoing["op"] == "COPY_OUT" and incoming["op"] == "COPY_IN"
                and outgoing["end"] == link["copy_out_end"]
                and incoming["start"] == link["copy_in_start"]
                and incoming["end"] == link["copy_in_end"]
                and link["copy_in_release"] == outgoing["end"] + 500
                and incoming["start"] >= link["copy_in_release"]):
            raise ValueError("saved COPY timeline/release mismatch")
        source_ops = sorted(o for o in producers[tid] if assignment[o] == src)
        targets = sorted((o for o in consumers[tid] if assignment[o] == dst),
                         key=lambda o: (-original[o]["end"], o))
        if len(source_ops) != 1 or not targets:
            skipped.append({"route": key, "reason": "producer is not unique on source core or no target consumer"})
            continue
        routes.append({**link, "producer": source_ops[0], "consumers": targets,
                       "queue_wait": incoming["start"] - link["copy_in_release"],
                       "distance_to_makespan": makespan - incoming["end"]})
    expected = {(tid, s, t) for tid in ir.tensors for s, t in _routes_for(tid, assignment, views)}
    if expected != {key for key in seen if key[0] in ir.tensors}:
        raise ValueError("saved transfer set does not match all original tensor core routes")
    return original, routes, {"scene": official["scene"], "makespan": makespan,
        "raw_transfer_count": len(raw), "mapped_transfer_count": len(routes), "skipped_transfers": skipped,
        "multi_consumer_routes": sum(len(r["consumers"]) > 1 for r in routes)}


def _chain(ir, seed, assignment, backwards, maximum=3):
    result, current = [seed], seed
    forward = ir.predecessors if backwards else ir.successors
    reverse = ir.successors if backwards else ir.predecessors
    while len(result) < maximum and len(forward[current]) == 1:
        other = next(iter(forward[current]))
        if len(reverse[other]) != 1 or assignment[other] != assignment[seed]:
            break
        result.append(other)
        current = other
    return result


def _communication_proposals(ir, assignment, routes, round_index, seed, limit):
    late = sorted(routes, key=lambda r: (-r["copy_in_end"], -r["queue_wait"], -r["size"], r["tensor_id"]))
    waiting = sorted(routes, key=lambda r: (-r["queue_wait"], -r["copy_in_end"], -r["size"], r["tensor_id"]))
    ranked, used = [], set()
    # Preserve four late routes before mixing high-wait routes; alternation on
    # later rounds broadens this fixed local neighbourhood without case rules.
    if round_index % 2:
        late, waiting = waiting, late
    seeds = late[:4] + waiting[:4] + late[4:limit] + waiting[4:limit]
    for route in seeds:
        key = route["tensor_id"], route["source_core"], route["target_core"]
        if key not in used:
            used.add(key)
            ranked.append(route)
    if ranked and seed:
        head = min(4, len(ranked))
        rotation = seed % head
        ranked[:head] = ranked[rotation:head] + ranked[:rotation]
    variants = ["producer_single", "consumer_single", "producer_chain", "consumer_chain"]
    shift = round_index % len(variants)
    variants = variants[shift:] + variants[:shift]
    proposals = []
    for variant in variants:
        for rank, route in enumerate(ranked[:limit]):
            producer_side = variant.startswith("producer")
            op = route["producer"] if producer_side else route["consumers"][0]
            destination = route["target_core"] if producer_side else route["source_core"]
            moving = _chain(ir, op, assignment, producer_side) if variant.endswith("chain") else [op]
            proposals.append(({o: destination for o in moving},
                {"strategy": variant, "route_rank": rank + 1, "seed_route": route,
                 "ranking_feature": "late_copy_or_release_queue_wait_not_exact_critical_path"}))
    return proposals


def _load_proposals(ir, assignment, timeline, num_cores, views, limit):
    loads = [defaultdict(int) for _ in range(num_cores)]
    groups = defaultdict(list)
    for op in ir.compute_ids:
        pipe = ir.ops[op]["pipe"]
        loads[assignment[op]][pipe] += max(1, ir.ops[op]["cycles"])
        groups[assignment[op], pipe].append(op)
    hotspots = sorted(groups, key=lambda k: (-loads[k[0]][k[1]], k))
    moves, swaps = [], []
    for source, pipe in hotspots[:min(6, len(hotspots))]:
        heavy = sorted(groups[source, pipe], key=lambda o: (-ir.ops[o]["cycles"], -timeline[o]["end"], o))[:6]
        targets = sorted((c for c in range(num_cores) if c != source), key=lambda c: (loads[c][pipe], c))[:2]
        peak = max(load[pipe] for load in loads)
        for target in targets:
            light = sorted(groups[target, pipe], key=lambda o: (ir.ops[o]["cycles"], -timeline[o]["end"], o))[:4]
            for op in heavy:
                work = max(1, ir.ops[op]["cycles"])
                after = [load[pipe] for load in loads]
                after[source] -= work
                after[target] += work
                gain = peak - max(after)
                if gain > 0:
                    changes = {op: target}
                    delta = _traffic_delta(ir, assignment, changes, views)
                    moves.append(((-gain, delta, -timeline[op]["end"], op, target), changes,
                                  {"strategy": "hot_pipe_single_move", "pipe": pipe,
                                   "static_pipe_peak_reduction": gain}))
                for other in light:
                    other_work = max(1, ir.ops[other]["cycles"])
                    after[source] = loads[source][pipe] - work + other_work
                    after[target] = loads[target][pipe] + work - other_work
                    gain = peak - max(after)
                    if gain > 0:
                        changes = {op: target, other: source}
                        delta = _traffic_delta(ir, assignment, changes, views)
                        swaps.append(((-gain, delta, op, other), changes,
                                      {"strategy": "hot_pipe_swap", "pipe": pipe,
                                       "static_pipe_peak_reduction": gain}))
    moves.sort(key=lambda r: r[0])
    swaps.sort(key=lambda r: r[0])
    # Swaps must survive behind many apparently attractive single moves.
    result = []
    for i in range(limit):
        for pool in (moves, swaps):
            if i < len(pool):
                result.append((pool[i][1], pool[i][2]))
    return result, [{"core": i, "cycles_by_pipe": dict(load)} for i, load in enumerate(loads)]


def _priority_orders(ir, assignment, round_index):
    stable = _topology(ir)
    tail, transfer_tail = {}, {}
    for op in reversed(stable):
        work = max(1, ir.ops[op]["cycles"])
        tail[op] = work + max((tail[n] for n in ir.successors[op]), default=0)
        transfer_tail[op] = work + max((transfer_tail[n] + (500 if assignment[op] != assignment[n] else 0)
                                     for n in ir.successors[op]), default=0)
    orders = [("compute_dependency_tail", _topology(ir, {o: (-tail[o], o) for o in stable})),
              ("cross_release_dependency_tail", _topology(ir, {o: (-transfer_tail[o], o) for o in stable})),
              ("stable_id", stable)]
    shift = round_index % len(orders)
    return orders[shift:] + orders[:shift]


def generate_trace_candidates(ir, plan, official_result, *, num_cores,
                              max_candidates=12, round_index=0, seed=0):
    """Return (list[{name, plan, metadata}], diagnostics); never evaluate/write.

    Plan and result must describe the same officially successful P2/P3 execution.
    This module validates ids/routes/timelines; the controller must additionally
    check source hashes and that the result's status was success. Empty active
    cores are supported. Invalid input raises ValueError; unsupported tensor
    attribution is recorded and omitted while other neighbourhoods remain.
    """
    if type(num_cores) is not int or not 1 <= num_cores <= 5:
        raise ValueError("num_cores must be 1..5")
    if type(max_candidates) is not int or max_candidates < 0:
        raise ValueError("max_candidates must be a nonnegative integer")
    if type(round_index) is not int or round_index < 0 or type(seed) is not int:
        raise ValueError("round_index must be nonnegative and seed must be an integer")
    assignment = _plan_assignment(ir, plan, num_cores)
    order, order_source, fallback = _recover_order(ir, plan, assignment)
    views = _tensor_views(ir)
    timeline, routes, trace_diagnostics = _trace(ir, assignment, official_result, num_cores, views, plan)
    boundaries = {int(op): sg for op, sg in plan["node_to_subgraph"].items()}
    encoded = _runs_plan(ir, order, assignment, num_cores, boundary_by_op=boundaries)
    original_hash = _fingerprint(plan)
    control_changed = _fingerprint(encoded) != original_hash
    diagnostics = {"schema_version": 1, "round_index": round_index, "seed": seed,
        "num_cores": num_cores, "max_candidates": max_candidates, "input_plan_sha256": original_hash,
        "order_source": order_source, "order_fallback": fallback,
        "order_sha256": _fingerprint(order), "base_reencoding_changes_plan": control_changed,
        "base_reencoding_may_change_priority": control_changed,
        "migration_preserves_original_sg_boundaries_in_recovered_order": True,
        "trace": trace_diagnostics, "exact_critical_path_claimed": False,
        "limitations": ["original dependency tail is not full compiled execution dependency graph",
                        "memory-reuse edges and dynamic DDR/MTE contention omitted from ranking",
                        "P3 L2 state is observed by official evaluator, not predicted here",
                        "dictionary serialization does not determine full original FIFO schedule",
                        "structural route bytes exclude input replication, final writes and spill"],
        "discarded": [], "families": {}, "official_evaluation_required": True}
    if max_candidates == 0 or not ir.compute_ids:
        diagnostics["generated_count"] = 0
        return [], diagnostics
    candidates, seen = [], {original_hash}

    def emit(family, candidate, metadata):
        signature = _fingerprint(candidate)
        if signature in seen:
            diagnostics["discarded"].append({"family": family, "strategy": metadata.get("strategy"), "reason": "duplicate_plan"})
            return False
        try:
            validate_plan(ir, candidate)
        except ValueError as error:
            diagnostics["discarded"].append({"family": family, "strategy": metadata.get("strategy"),
                                             "reason": "structure_rejected", "error": str(error)})
            return False
        seen.add(signature)
        candidates.append({"name": "trace_r{:02d}_{:02d}_{}".format(round_index, len(candidates), metadata["strategy"]),
            "plan": candidate, "metadata": {"family": family, "round_index": round_index,
                "seed": seed, "plan_sha256": signature, "base_order_source": order_source,
                "base_order_sha256": _fingerprint(order), "reencoding_control_changed": control_changed,
                "only_official_success_can_be_accepted": True, **metadata}})
        return True

    if control_changed:
        emit("control", encoded, {"strategy": "reencoding_control", "assignment_unchanged": True,
                                  "reason": "separate possible priority change from migration effects"})
    limit = min(32, max(8, max_candidates * 2))
    comm = _communication_proposals(ir, assignment, routes, round_index, seed, limit)
    load, load_diagnostics = _load_proposals(ir, assignment, timeline, num_cores, views, limit)
    diagnostics["static_compute_load"] = load_diagnostics
    queues = {"communication": deque(comm), "load": deque(load),
              "priority": deque(_priority_orders(ir, assignment, round_index))}
    # Fair allocation stops communication moves from consuming the whole budget.
    # Once one family is exhausted, remaining slots are filled by the others.
    while len(candidates) < max_candidates and any(queues.values()):
        for family in ("communication", "load", "priority"):
            if len(candidates) >= max_candidates:
                break
            accepted = False
            while queues[family] and not accepted:
                if family == "priority":
                    strategy, candidate_order = queues[family].popleft()
                    max_run = max(1, (len(order) + num_cores * 32 - 1) // (num_cores * 32))
                    candidate = _runs_plan(ir, candidate_order, assignment, num_cores, max_run_ops=max_run)
                    meta = {"strategy": strategy, "assignment_unchanged": True, "max_same_core_run_ops": max_run,
                            "candidate_order_sha256": _fingerprint(candidate_order),
                            "tail_omits_memory_dynamic_contention_and_l2": True}
                else:
                    changes, meta = queues[family].popleft()
                    changed = dict(assignment)
                    changed.update(changes)
                    candidate = _runs_plan(ir, order, changed, num_cores, boundary_by_op=boundaries)
                    meta = {**meta, "moved_ops": sorted(changes), "changes": [
                        {"op": o, "from_core": assignment[o], "to_core": changes[o]} for o in sorted(changes)],
                        "structural_cross_traffic_delta_bytes": _traffic_delta(ir, assignment, changes, views),
                        "fixed_global_order": True}
                    if family == "communication":
                        route = meta["seed_route"]
                        meta["seed_route_removed"] = (route["source_core"], route["target_core"]) not in _routes_for(
                            route["tensor_id"], changed, views)
                accepted = emit(family, candidate, meta)
    diagnostics["generated_count"] = len(candidates)
    diagnostics["families"] = dict(Counter(c["metadata"]["family"] for c in candidates))
    diagnostics["remaining_proposals_by_family"] = {f: len(q) for f, q in queues.items()}
    return candidates, diagnostics
