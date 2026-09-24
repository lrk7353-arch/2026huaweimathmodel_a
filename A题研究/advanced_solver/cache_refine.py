"""P3 proposals from verified COPY/FIFO timelines; never runs an evaluator.

Criticality below is an observed-timing heuristic, not a reconstruction of the
official execution DAG (memory reuse edges are absent from the public result).
Only the controller may accept a proposal, after official makespan evaluation.
"""
from collections import Counter, OrderedDict, defaultdict
import hashlib
import heapq
import json
import math
from pathlib import Path
import sys

_SOLVER = Path(__file__).resolve().parent.parent / "solver"
if str(_SOLVER) not in sys.path:
    sys.path.insert(0, str(_SOLVER))
from plan import validate_plan


class CacheResultMismatch(ValueError):
    """The supplied graph, plan and official P3 observation are inconsistent."""


def _require(condition, message):
    if not condition:
        raise CacheResultMismatch(message)


def _signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _core_map(plan):
    sg_core = {sg: core for core, order in enumerate(plan["core_schedules"]) for sg in order}
    return {int(op): sg_core[sg] for op, sg in plan["node_to_subgraph"].items()}


def _tensor_views(ir):
    producers, consumers = defaultdict(set), defaultdict(set)
    eligible = set(ir.compute_ids)
    for edge in ir.graph["edges"]:
        if edge["source"] in eligible:
            producers[edge["target"]].add(edge["source"])
        if edge["target"] in eligible:
            consumers[edge["source"]].add(edge["target"])
    return producers, consumers


def _validate_observation(ir, plan, result, num_cores):
    validate_plan(ir, plan)
    _require(len(plan["core_schedules"]) == num_cores, "plan core count differs from num_cores")
    _require(result.get("problem") == 3 and result.get("cache_mode") == "read_only", "a full official P3 result is required")
    _require(result.get("num_cores") == num_cores, "result core count differs from plan")
    for field, expected in (("bandwidth_bytes_per_cycle", 60), ("cache_capacity_bytes", 1048576),
                            ("cache_bandwidth_bytes_per_cycle", 250), ("cross_core_copy_delay_cycles", 500)):
        _require(result.get(field) == expected, "nonofficial or missing setting: " + field)
    _require(result.get("capacity_bytes") == {"L1": 524288, "UB": 131072}, "nonofficial L1/UB capacities")
    core_by_op = _core_map(plan)
    all_ops, compute, core_ends = {}, {}, {}
    timelines = result.get("per_core_timeline", [])
    _require(len(timelines) == num_cores, "timeline does not cover all configured cores")
    _require({c.get("core_id") for c in timelines} == set(range(num_cores)), "duplicate or missing timeline core")
    for core_entry in timelines:
        core = core_entry["core_id"]
        core_ends[core] = 0
        for op in core_entry.get("ops", []):
            key = core, op["op_id"]
            _require(key not in all_ops, "duplicate timeline op key")
            _require(all(isinstance(op.get(f), (int, float)) and not isinstance(op.get(f), bool)
                         and math.isfinite(op[f]) for f in ("start", "end", "duration")), "invalid timeline time")
            _require(0 <= op["start"] < op["end"] and op["duration"] == op["end"] - op["start"], "inconsistent timeline interval")
            all_ops[key] = op
            core_ends[core] = max(core_ends[core], op["end"])
            if op["op_id"] in core_by_op:
                ident = op["op_id"]
                _require(ident not in compute, "original compute op occurs more than once")
                _require(core_by_op[ident] == core and plan["node_to_subgraph"][str(ident)] == op["subgraph_id"],
                         "original compute op core/subgraph does not match plan")
                _require(op["op"] == ir.ops[ident]["op"] and op["pipe"] == ir.ops[ident]["pipe"], "original op type/pipe mismatch")
                _require(op["duration"] == max(1, ir.ops[ident]["cycles"]), "original compute duration mismatch")
                compute[ident] = op
            else:
                _require(op["op"] in ("COPY_IN", "COPY_OUT"), "unmapped non-COPY op in official timeline")
    _require(set(compute) == set(ir.compute_ids), "timeline does not cover original compute ops exactly")
    _require(result.get("makespan") == max(core_ends.values(), default=0), "makespan does not match timeline")
    for op, successors in ir.successors.items():
        for nxt in successors:
            _require(compute[op]["end"] <= compute[nxt]["start"], "timeline violates an original compute dependency")
    producers, consumers = _tensor_views(ir)
    expected_routes = {(tid, source, target) for tid in ir.tensors
                       for source in {core_by_op[p] for p in producers[tid]}
                       for target in {core_by_op[c] for c in consumers[tid]} if source != target}
    routes, route_by_read = {}, {}
    for route in result.get("cross_core_transfers", []):
        tid, source, target = route["tensor_id"], route["source_core"], route["target_core"]
        key = tid, source, target
        _require(tid in ir.tensors and key in expected_routes and key not in routes, "unknown/duplicate original tensor route")
        _require(route["size"] == ir.tensors[tid]["size"], "cross route tensor byte mismatch")
        write = all_ops.get((source, route["source_copy_out_id"]))
        read = all_ops.get((target, route["target_copy_in_id"]))
        _require(write is not None and write["op"] == "COPY_OUT" and read is not None and read["op"] == "COPY_IN", "cross route COPY join failed")
        _require(route["copy_out_end"] == write["end"] and route["copy_in_release"] == write["end"] + 500,
                 "cross route release is not COPY_OUT end + 500")
        _require(route["copy_in_start"] == read["start"] and route["copy_in_end"] == read["end"]
                 and read["start"] >= route["copy_in_release"], "cross route read timing mismatch")
        if read.get("cache_tensor_id") is not None:
            _require(read["cache_tensor_id"] == tid, "cross route logical tensor mismatch")
        _require(all(compute[p]["end"] <= write["start"] for p in producers[tid] if core_by_op[p] == source),
                 "cross COPY_OUT precedes its original producer")
        routes[key] = route
        route_by_read[target, read["op_id"]] = route
    _require(set(routes) == expected_routes, "cross transfer set differs from graph and plan")
    resident = OrderedDict()
    used, maximum_used = 0, 0
    seen, inserted_ever, access_keys = set(), set(), set()
    groups, classified = defaultdict(list), []
    counts, byte_counts = Counter(), Counter()
    first_insert, evictions, previous_time = {}, [], -1
    for index, raw in enumerate(result.get("cache_events", [])):
        event = dict(raw)
        kind, tid, size = event.get("event"), event.get("tensor_id"), event.get("size_bytes")
        core, op_id, now = event.get("core_id"), event.get("op_id"), event.get("time")
        _require(tid in ir.tensors and size == ir.tensors[tid]["size"], "cache logical tensor is not an original tensor or byte count differs")
        op = all_ops.get((core, op_id))
        _require(op is not None and op["op"] == "COPY_IN", "cache event COPY_IN join failed")
        _require(isinstance(now, (int, float)) and math.isfinite(now) and now >= previous_time, "cache event order/time invalid")
        previous_time = now
        if kind == "insert":
            _require(now == op["end"] and tid not in resident and size <= 1048576, "invalid cache insertion")
            if op.get("cache_tensor_id") is not None:
                _require(op["cache_tensor_id"] == tid, "insertion logical tensor differs from its COPY access")
            expected_evicted = []
            while resident and used + size > 1048576:
                old, old_size = resident.popitem(last=False)
                used -= old_size
                expected_evicted.append(old)
            _require(event.get("evicted_tensor_ids") == expected_evicted, "FIFO eviction order differs from official semantics")
            resident[tid] = size
            used += size
            _require(event.get("used_bytes") == used, "cache occupancy mismatch")
            maximum_used = max(maximum_used, used)
            inserted_ever.add(tid)
            first_insert.setdefault(tid, now)
            evictions.extend({"time": now, "tensor_id": old, "inserted_tensor": tid} for old in expected_evicted)
            continue
        _require(kind in ("hit", "miss") and size > 0 and now == op["start"], "invalid cache access event")
        _require((core, op_id) not in access_keys, "duplicate cache access")
        access_keys.add((core, op_id))
        _require(op.get("cache_tensor_id") == tid and op.get("cache_hit") == (kind == "hit"), "cache access and timeline disagree")
        _require(op.get("memory_path") == ("CACHE_READ" if kind == "hit" else "DDR"), "cache memory path mismatch")
        if kind == "hit":
            _require(tid in resident, "cache hit references an absent FIFO entry")
            category = "hit"
        else:
            _require(tid not in resident, "cache miss references a resident FIFO entry")
            if size > 1048576:
                category = "oversize_miss"
            elif tid not in seen:
                category = "first_access_miss"
            elif tid not in inserted_ever:
                _require(any(e["end"] > now for e in groups[tid]), "repeated cold miss has no earlier in-flight read")
                category = "concurrent_cold_miss"
            else:
                category = "post_eviction_miss"
        counts[category] += 1
        byte_counts[category] += size
        event.update(category=category, end=op["end"], duration=op["duration"], event_index=index)
        groups[tid].append(event)
        classified.append(event)
        seen.add(tid)
    expected_access_keys = {key for key, op in all_ops.items() if op.get("cache_tensor_id") is not None}
    _require(access_keys == expected_access_keys, "cache events do not cover COPY access timeline")
    _require(result.get("cache_used_bytes_final") == used, "final cache occupancy mismatch")
    _require(result.get("cache_final_entries") == [
        {"tensor_id": tid, "size_bytes": size} for tid, size in resident.items()], "final FIFO contents/order mismatch")
    stats = result.get("cache_stats", {})
    hits = counts["hit"]
    misses = len(classified) - hits
    hit_bytes, miss_bytes = byte_counts["hit"], sum(byte_counts.values()) - byte_counts["hit"]
    for field, value in (("copy_in_hits", hits), ("copy_in_misses", misses), ("hit_bytes", hit_bytes), ("miss_bytes", miss_bytes)):
        _require(stats.get(field) == value, "cache aggregate mismatch: " + field)
    rate = hit_bytes / (hit_bytes + miss_bytes) if hit_bytes + miss_bytes else 0.0
    _require(abs(stats.get("hit_rate", -1) - rate) < 1e-12, "cache hit-rate mismatch")
    return {"core_by_op": core_by_op, "compute": compute, "all_ops": all_ops, "core_ends": core_ends,
            "producers": producers, "consumers": consumers, "routes": routes, "route_by_read": route_by_read,
            "groups": groups, "accesses": classified, "counts": counts, "byte_counts": byte_counts,
            "first_insert": first_insert, "evictions": evictions, "max_cache_used_bytes": maximum_used}


def _observed_order_and_tails(ir, view):
    compute, assignment = view["compute"], view["core_by_op"]
    order = sorted(ir.compute_ids, key=lambda op: (compute[op]["start"], compute[op]["end"], assignment[op], op))
    rank = {op: i for i, op in enumerate(order)}
    successors = {op: set(ir.successors[op]) for op in order}
    pipe_previous = {}
    for op in order:
        key = assignment[op], compute[op]["pipe"]
        if key in pipe_previous:
            previous = pipe_previous[key]
            _require(compute[previous]["end"] <= compute[op]["start"], "overlapping same-core compute Pipe")
            successors[previous].add(op)
        pipe_previous[key] = op
    tails = {}
    for op in reversed(order):
        _require(all(rank[nxt] > rank[op] for nxt in successors[op]), "observed order is not topological")
        tails[op] = compute[op]["duration"] + max((tails[nxt] for nxt in successors[op]), default=0)
    return order, rank, tails


def _priority_order(ir, base_rank, priority):
    indegree = {op: len(ir.predecessors[op]) for op in ir.compute_ids}
    ready = [(priority.get(op, base_rank[op]), base_rank[op], op) for op in ir.compute_ids if indegree[op] == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        _, _, op = heapq.heappop(ready)
        order.append(op)
        for nxt in ir.successors[op]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(ready, (priority.get(nxt, base_rank[nxt]), base_rank[nxt], nxt))
    _require(len(order) == len(ir.compute_ids), "priority ordering did not cover DAG")
    return order


def _runs_plan(ir, order, assignment, num_cores):
    mapping, schedules = {}, [[] for _ in range(num_cores)]
    # Single-op intervals preserve each proposed within-core priority. Merging
    # consecutive same-core operations would erase the ordering inside a bucket.
    for sg, op in enumerate(order):
        core = assignment[op]
        schedules[core].append(sg)
        mapping[str(op)] = sg
    plan = {"node_to_subgraph": mapping, "core_schedules": schedules}
    validate_plan(ir, plan)
    return plan


def generate_cache_candidates(ir, plan, official_result, *, num_cores, max_candidates=12, round_index=0, seed=0):
    """Return candidate dictionaries and JSON-serializable diagnostics.

    The input result must be the FULL P3 result belonging to ``ir`` and ``plan``.
    The caller must additionally bind the record's graph/plan/config/code hashes;
    raw official results have no cryptographic provenance sufficient to do that.
    ``max_candidates`` includes the non-cache-specific re-encoding control.
    """
    if type(num_cores) is not int or not 1 <= num_cores <= 5:
        raise ValueError("num_cores must be 1..5")
    if type(max_candidates) is not int or max_candidates < 1:
        raise ValueError("max_candidates must be a positive integer")
    if type(round_index) is not int or round_index < 0 or type(seed) is not int:
        raise ValueError("round_index must be nonnegative and seed must be an integer")
    view = _validate_observation(ir, plan, official_result, num_cores)
    order, rank, tails = _observed_order_and_tails(ir, view)
    assignment, compute = view["core_by_op"], view["compute"]
    makespan = official_result["makespan"]
    ancestor_cache = {}

    def ancestors(op):
        if op not in ancestor_cache:
            reached, stack = set(), [op]
            while stack:
                current = stack.pop()
                if current not in reached:
                    reached.add(current)
                    stack.extend(ir.predecessors[current])
            ancestor_cache[op] = reached
        return ancestor_cache[op]

    accesses, targets = [], []
    for raw in view["accesses"]:
        event = dict(raw)
        tid, core = event["tensor_id"], event["core_id"]
        # Logical IDs survive spill renaming; select future consumers of this
        # incarnation, never confuse the COPY's priority subgraph with consumers.
        consumers = sorted((op for op in view["consumers"][tid] if assignment[op] == core
                            and compute[op]["start"] >= event["end"]), key=lambda op: (compute[op]["start"], op))
        event["future_consumer_ids"] = consumers
        if not consumers:
            event.update(criticality_proxy=0.0, consumer_gap_cycles=None, tail_slack_proxy_cycles=None,
                         nonterminal_hit=False)
            accesses.append(event)
            continue
        best_consumer = min(consumers, key=lambda op: (makespan - compute[op]["start"] - tails[op],
                                                       compute[op]["start"] - event["end"], op))
        gap = compute[best_consumer]["start"] - event["end"]
        tail_slack = max(0, makespan - compute[best_consumer]["start"] - tails[best_consumer])
        urgency = tails[best_consumer] / max(1, makespan - compute[best_consumer]["start"])
        potential = max(0, event["duration"] - max(1, math.ceil(event["size_bytes"] / 250)))
        score = potential * urgency * potential / max(1, potential + gap)
        event.update(criticality_proxy=urgency, consumer_gap_cycles=gap, tail_slack_proxy_cycles=tail_slack,
                     target_consumer=best_consumer, read_saving_proxy_cycles=potential, priority_score=score,
                     nonterminal_hit=(event["event"] == "hit" and view["core_ends"][core] < makespan
                                      and tail_slack > 0))
        accesses.append(event)
        if (event["event"] == "miss" and len(view["groups"][tid]) > 1
                and event["category"] != "oversize_miss" and score > 0):
            targets.append(event)
    # Repeated reads of one (tensor, core, category) are represented by the
    # highest scored occurrence; seed only breaks exact score ties, not graph ID.
    targets.sort(key=lambda event: (-event["priority_score"],
                  _signature([seed, round_index, event["tensor_id"], event["core_id"], event["op_id"]])))
    unique_targets, target_keys = [], set()
    for event in targets:
        key = event["tensor_id"], event["core_id"], event["category"]
        if key not in target_keys:
            target_keys.add(key)
            unique_targets.append(event)
    if unique_targets:
        offset = round_index % len(unique_targets)
        unique_targets = unique_targets[offset:] + unique_targets[:offset]
    candidates, seen, rejected, duplicates = [], set(), [], []

    def add(name, new_order, new_assignment, mechanism, target=None):
        if len(candidates) >= max_candidates:
            return
        try:
            candidate_plan = _runs_plan(ir, new_order, new_assignment, num_cores)
            signature = _signature(candidate_plan)
            if signature in seen:
                duplicates.append(name)
                return
            seen.add(signature)
            changed_cores = [op for op in order if new_assignment[op] != assignment[op]]
            metadata = {"family": "cache_refine", "mechanism": mechanism, "round_index": round_index,
                        "seed": seed, "is_reencoding_control": target is None,
                        "active_cores": len(set(new_assignment.values())),
                        "changed_core_count": len(changed_cores), "changed_core_ops": changed_cores,
                        "changed_global_rank_count": sum(a != b for a, b in zip(order, new_order)),
                        "plan_hash": signature, "target": target,
                        "selection_requirement": "accept only after official makespan evaluation; hit rate is diagnostic",
                        "structural_guarantee": "single-op topological intervals + increasing per-core interval order only; official execution still required"}
            candidates.append({"name": name, "plan": candidate_plan, "metadata": metadata})
        except (ValueError, AssertionError) as error:
            rejected.append({"name": name, "error": str(error)})

    add("cache_reencode_control", order, assignment, "observed_compute_order_reencoding")
    per_core = {core: [op for op in order if assignment[op] == core] for core in range(num_cores)}

    def lifted(consumer, anchor):
        priority = {op: min(rank[op], rank[anchor] - 0.25) for op in ancestors(consumer)}
        return _priority_order(ir, rank, priority)

    for target_index, target in enumerate(unique_targets[:max(4, 2 * max_candidates)]):
        if len(candidates) >= max_candidates:
            break
        tid, core, consumer = target["tensor_id"], target["core_id"], target["target_consumer"]
        label = "cache_r{:02d}_t{:03d}".format(round_index, target_index)
        meta_target = {key: target[key] for key in ("tensor_id", "core_id", "op_id", "time", "end", "category",
                                                  "priority_score", "target_consumer", "future_consumer_ids",
                                                  "tail_slack_proxy_cycles", "consumer_gap_cycles")}
        local = per_core[core]
        index = local.index(consumer)
        anchor = local[max(0, index - (2 + min(round_index, 3) * 2))]
        if target["category"] == "post_eviction_miss":
            add(label + "_advance_evicted_read", lifted(consumer, anchor), assignment,
                "advance_consumer_and_required_ancestors_before_fifo_reuse", meta_target)
        alternative_reads = [event for event in view["groups"][tid] if event["core_id"] != core]
        leader = min(alternative_reads, key=lambda event: (event["end"], event["time"], event["core_id"], event["op_id"])) if alternative_reads else None
        if leader is not None:
            leader_core = leader["core_id"]
            leader_consumers = sorted((op for op in view["consumers"][tid] if assignment[op] == leader_core
                                       and compute[op]["start"] >= leader["end"]), key=lambda op: (compute[op]["start"], op))
            if leader_consumers:
                lead_consumer = leader_consumers[0]
                lead_order = per_core[leader_core]
                lead_index = lead_order.index(lead_consumer)
                lead_anchor = lead_order[max(0, lead_index - (2 + min(round_index, 3) * 2))]
                add(label + "_advance_leader", lifted(lead_consumer, lead_anchor), assignment,
                    "advance_existing_reader_without_inserting_prefetch", {**meta_target, "leader_core": leader_core})
            # A short, existing independent operation can occupy useful work on
            # a follower while a leader read finishes. It is not a sleep or wait.
            blocked_readers = set(view["consumers"][tid])
            possible = [op for op in local[index + 1:] if op not in blocked_readers and op not in ancestors(consumer)]
            possible.sort(key=lambda op: (compute[op]["duration"], rank[op] - rank[consumer], op))
            for warmup in possible[:8]:
                if not (ancestors(warmup) & blocked_readers):
                    add(label + "_useful_work_before_follower", lifted(warmup, consumer), assignment,
                        "advance_existing_independent_work_before_follower", {**meta_target, "warmup_op": warmup,
                         "leader_core": leader_core, "warmup_ancestor_count": len(ancestors(warmup))})
                    break
            # Move a small complete same-core consumer set when possible. If it
            # is large, move only the critical consumer and admit route may remain.
            local_consumers = sorted(op for op in view["consumers"][tid] if assignment[op] == core)
            moving = local_consumers if len(local_consumers) <= 4 else [consumer]
            moved = dict(assignment)
            for op in moving:
                moved[op] = leader_core
            add(label + "_colocate_readers", order, moved, "small_reuse_consumer_colocation",
                {**meta_target, "destination_core": leader_core, "all_local_consumers_moved": len(moving) == len(local_consumers),
                 "moved_consumer_ops": moving})
        elif target["category"] != "post_eviction_miss":
            add(label + "_advance_read", lifted(consumer, anchor), assignment,
                "advance_existing_repeated_read_consumers", meta_target)
    diagnostics = {
        "schema_version": 1, "result_validation": "passed", "input_plan_hash": _signature(plan),
        "input_makespan": makespan, "num_cores": num_cores, "round_index": round_index, "seed": seed,
        "cache_access_counts": dict(view["counts"]), "cache_access_bytes": dict(view["byte_counts"]),
        "fifo_eviction_count": len(view["evictions"]), "max_cache_used_bytes": view["max_cache_used_bytes"],
        "cross_routes_verified": len(view["routes"]), "logical_tensor_groups": len(view["groups"]),
        "repeated_logical_tensors": sum(len(group) > 1 for group in view["groups"].values()),
        "hits_on_nonterminal_cores_with_slack": sum(event.get("nonterminal_hit", False) for event in accesses),
        "unmapped_future_consumer_accesses": sum(not event["future_consumer_ids"] for event in accesses),
        "ranked_target_count": len(unique_targets), "ranked_targets": unique_targets[:max(24, 2 * max_candidates)],
        "access_examples": accesses[:12], "eviction_examples": view["evictions"][:12],
        "candidate_count": len(candidates), "duplicate_proposals": duplicates, "rejected_proposals": rejected,
        "criticality_scope": "heuristic using observed compute dependencies and same-Pipe tail work; no full memory/DDR causal DAG",
        "provenance_boundary": "caller must bind raw result to graph/plan/config/code hashes; this module validates identities and timing consistency",
        "no_evaluation_performed": True,
    }
    return candidates, diagnostics
