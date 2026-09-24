"""Bounded whole-component reference portfolio; no simulator calls or case-ID rules.

The two resource proxies distinguish P1 Task reads from P2/P3 core reads.
They exclude spill, dynamic bandwidth sharing, Pipe FIFO effects and L2 timing,
and therefore must not be reported as official makespans or lower bounds.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json

from solver.baselines import generate_candidates as legacy_candidates
from solver.common import single_active_plan
from solver.plan import plan_from_component_groups, validate_plan


PROXY_DDR_BYTES_PER_CYCLE = 60.0
PROXY_P1_TASK_SWITCH_CYCLES = 100
SECOND_START_SEED_OFFSET = 104729


def _fingerprint(plan):
    # Keep mapping insertion order: evaluator construction can observe it.
    return hashlib.sha256(json.dumps(plan, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _component_plan_view(ir, plan):
    """Return exact core -> Task -> WCC order, retaining mapping insertion order."""
    sg_components, component_owner = {}, {}
    if set(map(int, plan["node_to_subgraph"])) != set(ir.compute_ids):
        raise ValueError("component proxy requires exact non-COPY coverage")
    for raw_op, sg in plan["node_to_subgraph"].items():
        cid = ir.component_by_op[int(raw_op)]
        if cid in component_owner and component_owner[cid] != sg:
            raise ValueError("component proxy requires every WCC to remain in one subgraph")
        if cid not in component_owner:
            sg_components.setdefault(sg, []).append(cid)
            component_owner[cid] = sg
    groups = tuple(tuple(tuple(sg_components[sg]) for sg in schedule)
                   for schedule in plan["core_schedules"])
    assignment = [None] * len(ir.components)
    for core, tasks in enumerate(groups):
        for cids in tasks:
            for cid in cids:
                assignment[cid] = core
    return groups, tuple(assignment)


def _original_output_bytes(ir):
    copy_out_ids = {op["id"] for op in ir.graph["ops"] if op["op"] == "COPY_OUT"}
    return sum(ir.tensors[e["target"]]["size"] for e in ir.graph["edges"]
               if e["source"] in copy_out_ids and e["target"] in ir.tensors
               and ir.tensors[e["target"]]["pos"] == "DDR")


def component_proxy(ir, plan):
    """Explain a whole-WCC plan with separate per-core and per-Task read counts.

    Public for diagnostics/tests. Root reads count each logical root once in a
    core/Task, not once per consumer edge. Work is two-dimensional M/V; Other
    work is retained as a third diagnostic instead of silently discarded.
    """
    groups, assignment = _component_plan_view(ir, plan)
    per_core, p1_core_envelopes = [], []
    all_roots, task_reads, core_reads = set(), 0, 0
    for core, tasks in enumerate(groups):
        root_union = set()
        m = v = other = 0
        serial_task_compute = 0
        for cids in tasks:
            task_roots = set()
            tm = tv = to = 0
            for cid in cids:
                component = ir.components[cid]
                tm += component.work_m
                tv += component.work_v
                to += component.work_other
                task_roots.update(component.input_ids)
            m, v, other = m + tm, v + tv, other + to
            serial_task_compute += max(tm, tv, to)
            task_reads += sum(ir.input_sizes[t] for t in task_roots)
            root_union.update(task_roots)
        read_bytes = sum(ir.input_sizes[t] for t in root_union)
        core_reads += read_bytes
        all_roots.update(root_union)
        switches = max(0, len(tasks) - 1) * PROXY_P1_TASK_SWITCH_CYCLES
        p1_core_envelopes.append(serial_task_compute + switches)
        per_core.append({"core": core, "work_m": m, "work_v": v, "work_other": other,
                         "tasks": len(tasks), "root_read_bytes": read_bytes,
                         "p1_task_serial_envelope": serial_task_compute + switches})
    unique_reads = sum(ir.input_sizes[t] for t in all_roots)
    output_bytes = _original_output_bytes(ir)
    p2_compute = max((max(c["work_m"], c["work_v"], c["work_other"]) for c in per_core), default=0)
    return {"core_work": per_core, "component_assignment": list(assignment),
            "active_cores": sum(bool(tasks) for tasks in groups),
            "tasks": sum(len(tasks) for tasks in groups),
            "root_unique_bytes": unique_reads,
            "root_read_bytes_per_core_total": core_reads,
            "root_read_bytes_per_task_total": task_reads,
            "root_replication_bytes_p1": task_reads - unique_reads,
            "root_replication_bytes_p2": core_reads - unique_reads,
            "original_final_output_bytes": output_bytes,
            "p1_proxy_cycles": max(max(p1_core_envelopes, default=0),
                                   (task_reads + output_bytes) / PROXY_DDR_BYTES_PER_CYCLE),
            "p2_proxy_cycles": max(p2_compute, (core_reads + output_bytes) / PROXY_DDR_BYTES_PER_CYCLE),
            "proxy_is_official_makespan": False,
            "proxy_is_certified_lower_bound": False,
            "proxy_excludes": ["spill and tensor lifetime", "dynamic DDR contention", "Pipe FIFO",
                               "precise transfer/compute overlap", "L2 hit/eviction timing"],
            "proxy_fixed_parameters": {"DDR_bytes_per_cycle": PROXY_DDR_BYTES_PER_CYCLE,
                                       "P1_same_core_task_wait": PROXY_P1_TASK_SWITCH_CYCLES}}


def _features(ir, num_cores):
    total_m = sum(c.work_m for c in ir.components)
    total_v = sum(c.work_v for c in ir.components)
    ratios = [c.work_m / max(1, c.work_m + c.work_v) for c in ir.components]
    shared = {t: cids for t, cids in ir.input_components.items() if len(cids) > 1}
    return {"components": len(ir.components), "requested_cores": num_cores,
            "max_active_cores": min(num_cores, len(ir.components)),
            "shared_root_count": len(shared),
            "shared_root_bytes": sum(ir.input_sizes[t] for t in shared),
            "max_root_component_fanout": max((len(v) for v in shared.values()), default=0),
            "work_m": total_m, "work_v": total_v,
            "mv_component_ratio_range": max(ratios, default=0) - min(ratios, default=0),
            "largest_component_work_fraction": max((c.compute_work for c in ir.components), default=0)
            / max(1, sum(c.compute_work for c in ir.components))}


def _load_order(ir, cids):
    return sorted(cids, key=lambda cid: (-max(ir.components[cid].work_m,
                                             ir.components[cid].work_v,
                                             ir.components[cid].work_other), cid))


def _ordered_components(ir, cids, ordering):
    if ordering == "load":
        return _load_order(ir, cids)
    if ordering == "shared_root":
        def key(cid):
            roots = [t for t in ir.components[cid].input_ids if len(ir.input_components[t]) > 1]
            if not roots:
                return (1, 0, 0, -ir.components[cid].compute_work, cid)
            lead = min(roots, key=lambda t: (-ir.input_sizes[t] * (len(ir.input_components[t]) - 1), t))
            return (0, lead, -ir.input_sizes[lead], -ir.components[cid].compute_work, cid)
        return sorted(cids, key=key)
    if ordering != "mv_complement":
        raise ValueError("unknown component ordering")
    # Merge two load-ranked queues, selecting the next item that keeps normalized
    # cumulative M/V progress closest. Unlike a scalar sum, complementary pipes
    # can share a Task without summing their nominal compute envelopes.
    tm = max(1, sum(ir.components[c].work_m for c in cids))
    tv = max(1, sum(ir.components[c].work_v for c in cids))
    queues = [[], []]
    for cid in _load_order(ir, cids):
        c = ir.components[cid]
        queues[0 if c.work_m / tm >= c.work_v / tv else 1].append(cid)
    cursors, m, v, result = [0, 0], 0, 0, []
    while len(result) < len(cids):
        choices = []
        for q in (0, 1):
            if cursors[q] < len(queues[q]):
                cid = queues[q][cursors[q]]
                c = ir.components[cid]
                choices.append((abs((m + c.work_m) / tm - (v + c.work_v) / tv), cid, q))
        _, cid, q = min(choices)
        cursors[q] += 1
        result.append(cid)
        m += ir.components[cid].work_m
        v += ir.components[cid].work_v
    return result


def _work_chunks(ir, ordered, requested):
    """At most requested nonempty consecutive whole-WCC chunks, by work mass."""
    if not ordered:
        return ()
    count = min(requested, len(ordered))
    weights = {cid: max(1, ir.components[cid].work_m, ir.components[cid].work_v,
                        ir.components[cid].work_other) for cid in ordered}
    remaining_work = sum(weights.values())
    result, cursor = [], 0
    for chunk in range(count):
        groups_left = count - chunk
        if groups_left == 1:
            result.append(tuple(ordered[cursor:]))
            break
        target, members, work = remaining_work / groups_left, [], 0
        while cursor < len(ordered) - (groups_left - 1):
            cid = ordered[cursor]
            cursor += 1
            members.append(cid)
            work += weights[cid]
            if work >= target:
                break
        result.append(tuple(members))
        remaining_work -= work
    return tuple(result)


def _granularity_bucket(groups):
    if all(len(tasks) <= 1 for tasks in groups):
        return "per_core"
    if all(len(cids) == 1 for tasks in groups for cids in tasks):
        return "per_component"
    return "batched_components"


def _assignment_signature(assignment):
    canonical = {}
    return tuple(canonical.setdefault(core, len(canonical)) for core in assignment)


def generate_component_candidates(ir, num_cores, *, max_candidates=12, seed=0):
    """Generate a deterministic bounded portfolio of legal whole-WCC plans.

    No official evaluation occurs here. The returned ranking uses only graph
    structure and proxy features, never path/case ID, timings or stored scores.
    """
    if type(num_cores) is not int or not 1 <= num_cores <= 5:
        raise ValueError("num_cores must be an integer in 1..5")
    if type(max_candidates) is not int or max_candidates < 1:
        raise ValueError("max_candidates must be a positive integer")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    features = _features(ir, num_cores)
    second_start = (features["components"] >= max(2, num_cores)
                    and (features["shared_root_count"] > 0
                         or features["mv_component_ratio_range"] >= 0.35))
    seeds = [seed, seed + SECOND_START_SEED_OFFSET] if second_start else [seed]
    pool, by_fingerprint, raw_count = [], {}, 0

    def add(name, plan, metadata):
        nonlocal raw_count
        raw_count += 1
        fingerprint = _fingerprint(plan)
        if fingerprint in by_fingerprint:
            by_fingerprint[fingerprint]["metadata"]["equivalent_origins"].append(name)
            return by_fingerprint[fingerprint]
        groups, assignment = _component_plan_view(ir, plan)
        proxy = component_proxy(ir, plan)
        row = {"name": name, "plan": plan,
               "metadata": {"family": "whole_component_reference", "seed": seed,
                            "splits_components": False, "official_evaluation_required": True,
                            "granularity": _granularity_bucket(groups),
                            "proxy": proxy, "equivalent_origins": [], **metadata},
               "_groups": groups, "_assignment": assignment, "_fingerprint": fingerprint,
               "_assignment_signature": _assignment_signature(assignment)}
        pool.append(row)
        by_fingerprint[fingerprint] = row
        return row

    legacy = []
    for start_index, local_seed in enumerate(seeds):
        generated = legacy_candidates(ir, num_cores, method="affinity", seed=local_seed)
        for item in generated:
            origin = item["metadata"]["source"]
            row = add(f"wcc_start{start_index}_{item['name']}", item["plan"],
                      {"source": "legacy_" + origin, "ordering": "legacy",
                       "start_index": start_index, "start_seed": local_seed,
                       "legacy_metadata": item["metadata"]})
            legacy.append(row)

    safe = add("whole_graph_single_active", single_active_plan(ir.graph, num_cores),
               {"source": "whole_graph_reference", "ordering": "official_sorted_op_ids"})

    # For each active-core count, expand at most two distinct assignments:
    # one with best load/read envelope and one with fewest replicated root bytes.
    layouts = []
    for active in range(1, features["max_active_cores"] + 1):
        available = [r for r in legacy if r["metadata"]["proxy"]["active_cores"] == active
                     and r["metadata"]["granularity"] == "per_core"]
        if not available:
            continue
        choices = [min(available, key=lambda r: (r["metadata"]["proxy"]["p2_proxy_cycles"], r["name"]))]
        if features["shared_root_count"]:
            choices.append(min(available, key=lambda r: (r["metadata"]["proxy"]["root_replication_bytes_p2"],
                                                        r["metadata"]["proxy"]["p2_proxy_cycles"], r["name"])))
        seen_assignment = set()
        for row in choices:
            if row["_assignment"] not in seen_assignment:
                layouts.append(row)
                seen_assignment.add(row["_assignment"])

    for layout_index, layout in enumerate(layouts):
        assignment = layout["_assignment"]
        orderings = ["mv_complement"]
        if features["shared_root_count"]:
            orderings.append("shared_root")
        for ordering in orderings:
            ordered_by_core = [_ordered_components(ir, [cid for cid, c in enumerate(assignment) if c == core], ordering)
                               for core in range(num_cores)]
            granularities = ["per_component"]
            if max(map(len, ordered_by_core), default=0) >= 3:
                granularities += ["batch2", "batch4"]
            for granularity in granularities:
                if granularity == "per_component":
                    groups = tuple(tuple((cid,) for cid in ordered) for ordered in ordered_by_core)
                else:
                    groups = tuple(_work_chunks(ir, ordered, int(granularity[-1])) for ordered in ordered_by_core)
                plan = plan_from_component_groups(ir, groups)
                add(f"wcc_task_{layout_index}_{ordering}_{granularity}", plan,
                    {"source": "task_aware_component_layout", "ordering": ordering,
                     "task_grouping_rule": granularity,
                     "assignment_origin": layout["name"],
                     "structure_trigger": "shared_root" if ordering == "shared_root" else "whole_component_MV_complement"})

    p1_floor = max(1.0, min(r["metadata"]["proxy"]["p1_proxy_cycles"] for r in pool))
    p2_floor = max(1.0, min(r["metadata"]["proxy"]["p2_proxy_cycles"] for r in pool))
    for row in pool:
        p = row["metadata"]["proxy"]
        row["_quality"] = 0.5 * (p["p1_proxy_cycles"] / p1_floor + p["p2_proxy_cycles"] / p2_floor)
    selected, selected_hashes = [], set()

    def take(row, reason):
        if row["_fingerprint"] in selected_hashes or len(selected) >= max_candidates:
            return
        selected_hashes.add(row["_fingerprint"])
        row["metadata"]["selection_reason"] = reason
        row["metadata"]["selection_rank"] = len(selected)
        selected.append(row)

    full_lpt = [r for r in pool if r["metadata"].get("source") == "legacy_simple"
                and r["metadata"].get("start_index") == 0
                and r["metadata"]["proxy"]["active_cores"] == features["max_active_cores"]
                and r["metadata"]["granularity"] == "per_core"]
    take(min(full_lpt, key=lambda r: r["name"]) if full_lpt else safe, "max-active M/V LPT anchor")
    take(safe, "common whole-graph single-active reference")
    # Give task-aware layouts an early slot, then a different fine granularity.
    # This is fixed a priori, not chosen by observed official performance.
    for family, reason in (("batched_components", "intermediate Task-granularity control"),
                           ("per_component", "fine Task/priority-granularity control")):
        choices = [r for r in pool if r["metadata"]["granularity"] == family
                   and r["metadata"]["proxy"]["active_cores"] == features["max_active_cores"]]
        if choices:
            take(min(choices, key=lambda r: (r["_quality"], r["name"])), reason)
    # Diversity penalizes already-represented assignment, active count, Task
    # granularity and ordering; all coefficients are fixed, dimensionless.
    while len(selected) < min(max_candidates, len(pool)):
        assignment_counts = Counter(r["_assignment_signature"] for r in selected)
        active_counts = Counter(r["metadata"]["proxy"]["active_cores"] for r in selected)
        granularity_counts = Counter(r["metadata"]["granularity"] for r in selected)
        ordering_counts = Counter(r["metadata"]["ordering"] for r in selected)
        choices = [r for r in pool if r["_fingerprint"] not in selected_hashes]
        def key(row):
            m = row["metadata"]
            penalty = (0.25 * assignment_counts[row["_assignment_signature"]]
                       + 0.15 * active_counts[m["proxy"]["active_cores"]]
                       + 0.10 * granularity_counts[m["granularity"]]
                       + 0.05 * ordering_counts[m["ordering"]])
            return row["_quality"] + penalty, row["_quality"], row["name"]
        take(min(choices, key=key), "fixed joint-proxy plus structural-diversity ranking")

    for row in selected:
        validate_plan(ir, row["plan"])
        row["metadata"]["proxy_quality_normalized"] = row["_quality"]
    diagnostics = {"family": "whole_component_reference", "seed": seed, "max_candidates": max_candidates,
                   "features": features, "legacy_start_seeds": seeds, "legacy_generator_calls": len(seeds),
                   "second_start_triggered": second_start, "raw_candidates_considered": raw_count,
                   "unique_candidates_considered": len(pool), "selected_candidates": len(selected),
                   "selected_sources": dict(Counter(r["metadata"]["source"] for r in selected)),
                   "selected_active_cores": sorted({r["metadata"]["proxy"]["active_cores"] for r in selected}),
                   "selected_granularities": sorted({r["metadata"]["granularity"] for r in selected}),
                   "selection_uses_case_id": False, "official_calls": 0,
                   "guarantees": ["WCCs remain whole", "deterministic for fixed graph/seed", "bounded returned count"],
                   "not_guaranteed": ["official feasibility before evaluator", "official score dominance", "strict wallclock bound"],
                   "complexity": "At most 2 legacy portfolios; each legacy local search uses at most 2x64 component move passes and 2x96 swaps per start. Task layouts expand at most 2 assignments per active count, 2 orderings and 3 granularities. Each layout uses O(C log C + input incidences + V + E); selection is O(max_candidates * pool_size * C) worst case. No official simulation in generation.",
                   "comparison_note": "Charge evaluated candidates and generation walltime. Do not treat unseen or proxy-ranked candidates as measured incumbents."}
    return [{k: v for k, v in row.items() if not k.startswith("_")} for row in selected], diagnostics
