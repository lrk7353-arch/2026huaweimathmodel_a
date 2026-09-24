"""Operation assignment candidates with explicit proxy limitations (stdlib only)."""
from pathlib import Path
import heapq
import random
import sys

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "solver"), str(HERE.parent / "探索")]
from common import object_digest
from plan import validate_plan
from operation_heft_probe import dependency_views, operation_assignment, runs_plan
from partition_candidates import topological_order, contiguous_blocks


def memory_priority_order(ir, seed=0, randomized=False):
    """Kahn order favors long tails and releasing large internal inputs.

    This static priority neither predicts residency nor claims minimum memory.
    Jitter changes only ready-node priorities and is fixed by a local RNG.
    """
    base = topological_order(ir, "stable_id")
    tail = {}
    for op in reversed(base):
        tail[op] = max(1, ir.ops[op]["cycles"]) + max((tail[n] for n in ir.successors[op]), default=0)
    incoming, outgoing = {op: 0 for op in base}, {op: 0 for op in base}
    for e in ir.graph["edges"]:
        a, b = e["source"], e["target"]
        if a in ir.tensors and b in incoming and ir.tensors[a]["pos"] != "DDR":
            incoming[b] += ir.tensors[a]["size"]
        if a in outgoing and b in ir.tensors and ir.tensors[b]["pos"] != "DDR":
            outgoing[a] += ir.tensors[b]["size"]
    rng = random.Random(seed)
    jitter = {op: rng.uniform(.90, 1.10) if randomized else 1.0 for op in base}
    def key(op):
        # Byte/60 is a scale choice for ranking, not a memory or runtime proof.
        pressure = (incoming[op] - outgoing[op]) / 60.0
        return (-(tail[op] + pressure) * jitter[op], -incoming[op], op)
    indegree = {op: len(ir.predecessors[op]) for op in base}
    ready = [key(op) for op in base if indegree[op] == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        op = heapq.heappop(ready)[-1]
        order.append(op)
        for nxt in ir.successors[op]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(ready, key(nxt))
    if len(order) != len(base):
        raise ValueError("compute dependency cycle")
    return order


def generate_operation_candidates(ir, num_cores, *, max_candidates=12, seed=0):
    if type(num_cores) is not int or not 1 <= num_cores <= 5:
        raise ValueError("cores must be 1..5")
    if type(max_candidates) is not int or max_candidates < 1:
        raise ValueError("candidate limit must be positive")
    candidates, seen, failures, all_names = [], {}, [], []
    orders = {"critical_path": topological_order(ir, "critical_path"),
              "stable_id": topological_order(ir, "stable_id"),
              "release_priority": memory_priority_order(ir),
              "seeded_priority": memory_priority_order(ir, seed, True)}
    def add(name, plan, meta):
        all_names.append(name)
        validate_plan(ir, plan)
        signature = object_digest(plan)
        if signature in seen:
            seen[signature]["metadata"]["aliases"].append(name)
            return
        item = {"name": name, "plan": plan, "metadata": {**meta, "family": "operation",
                "seed": seed, "active_cores": sum(bool(c) for c in plan["core_schedules"]),
                "subgraphs": len(set(plan["node_to_subgraph"].values())), "aliases": []}}
        candidates.append(item); seen[signature] = item
    try:
        views = dependency_views(ir)
    except ValueError as e:
        views = None
        failures.append({"family": "communication_attribution", "error": str(e)})
    # Broad prefix: both established orders, then stronger communication and
    # pressure-aware candidates. No candidate ordering depends on official scores.
    specifications = [("critical_path", 1.0), ("stable_id", 1.0),
                      ("critical_path", 2.0), ("stable_id", 2.0),
                      ("release_priority", 1.0), ("seeded_priority", 1.0),
                      ("critical_path", .25), ("stable_id", .25),
                      ("release_priority", 2.0), ("seeded_priority", .5)]
    if views is not None:
        for ordering, weight in specifications:
            try:
                assignment, proxy = operation_assignment(ir, orders[ordering], weight, views, num_cores=num_cores)
                add("op_{}_w{:03d}".format(ordering, round(weight * 100)),
                    runs_plan(ir, orders[ordering], assignment, num_cores=num_cores),
                    {"ordering": ordering, "communication_weight": weight, **proxy})
            except ValueError as e:
                failures.append({"ordering": ordering, "weight": weight, "error": str(e)})
    # Priority-only controls can help even N=1, where run compression loses order
    # granularity. They preserve a legal global sequence with explicit buckets.
    if ir.compute_ids:
        for ordering in ("critical_path", "release_priority"):
            blocks = contiguous_blocks(ir, orders[ordering], max(8, 4 * num_cores))
            plan = {"node_to_subgraph": {str(op): i for i, block in enumerate(blocks) for op in block},
                    "core_schedules": [list(range(len(blocks)))] + [[] for _ in range(num_cores - 1)]}
            add("priority_only_" + ordering, plan,
                {"ordering": ordering, "family_detail": "one-core priority control"})
    omitted = [r["name"] for r in candidates[max_candidates:]]
    return candidates[:max_candidates], {"generated_unique": len(candidates), "attempted_names": all_names,
           "omitted_by_budget": omitted, "generation_failures": failures,
           "proxy_scope": "M/V availability, input-read cursor and deduplicated tensor routes; no dynamic DDR/FIFO/spill/Cache simulation"}
