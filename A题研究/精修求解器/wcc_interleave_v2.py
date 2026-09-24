"""WCC interleaving v2: preserve control priorities on ineligible cores.

Uses frozen v1 pure helpers, but changes which cores receive window scheduling.
No evaluator calls and no changes to frozen v1, official code, or input plans.
"""
from pathlib import Path
import sys

try:
    import wcc_interleave as v1
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import wcc_interleave as v1


def generate_interleave_candidates(ir, plan, *, num_cores, max_candidates=12, seed=17):
    """Emit one control and up to eight phase candidates, all with fixed cores.

    Only cores with >=2 complete WCCs and both M/V work are window-scheduled.
    Other cores keep exactly the control's per-core compute expression order.
    This preserves an expressed priority, not the original Step1 execution trace.
    Any cross-core WCC makes the whole input explicitly unsupported.
    """
    if type(num_cores) is not int or not 1 <= num_cores <= 5:
        raise ValueError("num_cores must be an integer in 1..5")
    if type(max_candidates) is not int or max_candidates < 0:
        raise ValueError("max_candidates must be a nonnegative integer")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    assignment = v1._assignment(ir, plan, num_cores)
    core_components = [[] for _ in range(num_cores)]
    split = []
    for component in ir.components:
        cores = {assignment[o] for o in component.nodes}
        if len(cores) != 1:
            split.append(component.id)
        else:
            core_components[next(iter(cores))].append(component.id)
    diag = {"schema_version": 2, "official_calls": 0, "seed": seed,
        "input_plan_sha256": v1._digest(plan), "max_candidates": max_candidates,
        "split_wcc_ids": split, "wcc_count_by_core": [len(c) for c in core_components],
        "applicable": False, "generated_count": 0, "discarded": [],
        "scope": "P2/P3 fixed-core priority; noneligible cores preserve control expression order",
        "limitations": ["control single-op SG encoding can change original Step1 behavior",
            "unchanged core priority does not imply unchanged timing under shared DDR",
            "window scheduling may also reorder within an eligible WCC",
            "window bounds WCC count, not capacity or real execution overlap",
            "v1 compute/memory proxies omit COPY, spill, memory-reuse, bandwidth and L2",
            "original official success required before accepting any plan"]}
    if split:
        diag["reason"] = "input contains cross-core WCCs; no partial rewrite attempted"
        return [], diag
    if any(ir.ops[o]["pipe"] not in v1.PIPES for o in ir.compute_ids):
        diag["reason"] = "non-COPY compute uses a pipe outside M/V"
        return [], diag
    eligible = [k for k, cs in enumerate(core_components) if len(cs) >= 2
                and sum(ir.components[c].work_m for c in cs) > 0
                and sum(ir.components[c].work_v for c in cs) > 0]
    preserved = [k for k in range(num_cores) if k not in eligible]
    diag.update(eligible_cores=eligible, preserved_cores=preserved)
    if not eligible:
        diag["reason"] = "no core has multiple complete WCCs with both M and V work"
        return [], diag
    diag["applicable"] = True
    if max_candidates == 0:
        return [], diag
    base_order, origin = v1._control_order(ir, plan, assignment)
    base_by_core = [[o for o in base_order if assignment[o] == k] for k in range(num_cores)]
    diag.update(control_order_source=origin, control_claims_exact_original_step1_sequence=False,
        preserved_core_priority_sha256={str(k): v1._digest(base_by_core[k]) for k in preserved})
    position = {o: i for i, o in enumerate(base_order)}
    first = {c.id: min(position[o] for o in c.nodes) for c in ir.components}
    tail = {}
    for o in reversed(base_order):
        tail[o] = max(1, ir.ops[o]["cycles"]) + max((tail[n] for n in ir.successors[o]), default=0)
    incident = v1._memory_views(ir)
    candidates, seen = [], set()
    def emit(name, order, metadata):
        encoded = v1._encode(ir, order, assignment, num_cores)
        h = v1._digest(encoded)
        if h in seen:
            diag["discarded"].append({"name": name, "reason": "exact_duplicate_plan"})
            return
        if v1._assignment(ir, encoded, num_cores) != assignment:
            raise AssertionError("fixed-core invariant violated")
        for core in preserved:
            if [o for o in order if assignment[o] == core] != base_by_core[core]:
                raise AssertionError("noneligible core priority changed")
        seen.add(h)
        candidates.append({"name": name, "plan": encoded, "metadata": {
            "family": "wcc_interleave_v2", "plan_sha256": h, "seed": seed,
            "assignment_unchanged": True, "original_op_count": len(ir.compute_ids),
            "subgraph_count": len(ir.compute_ids), "global_topology_sha256": v1._digest(order),
            "eligible_cores": eligible, "preserved_cores": preserved,
            "preserved_core_priority_sha256": diag["preserved_core_priority_sha256"],
            "noneligible_core_order_matches_control": True,
            "capacity_certified": False, "official_evaluation_required": True, **metadata}})
    emit("interleave_v2_reencode_control", base_order, {"strategy": "reencode_control",
        "order_source": origin, "window": None, "original_step1_order_preserved_claimed": False})
    specs = [(w, a, "earliest_start") for w in (2, 4, 8) for a in ("base", "complement")]
    specs += [(w, "complement", "memory_tie") for w in (2, 4)]
    for window, admission, dispatch in specs:
        if len(candidates) >= max_candidates:
            break
        whole_order, per_core = [], []
        for core, cs in enumerate(core_components):
            if core in eligible:
                local, description = v1._local_order(ir, cs, window, admission, dispatch,
                                                     first, tail, incident, seed)
                description.update(priority_mode="window", window_applies=True)
            else:
                local = base_by_core[core]
                description = {"priority_mode": "preserve_control", "window_applies": False,
                    "control_priority_sha256": v1._digest(local), "operation_count": len(local),
                    "predicted_compute_and_memory": None}
            whole_order.extend(local)
            per_core.append({"core": core, **description})
        emit(f"interleave_v2_w{window}_{admission}_{dispatch}", whole_order,
            {"strategy": "window_interleave", "window": window,
             "window_applies_only_to_eligible_cores": True,
             "admission": admission, "dispatch": dispatch, "per_core_proxy": per_core})
    diag["generated_count"] = len(candidates)
    return candidates, diag
