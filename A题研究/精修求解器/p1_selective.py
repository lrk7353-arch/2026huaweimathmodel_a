"""Selective P1 Task decomposition, preserving small independent components.

All scores used to rank candidates are heuristics except task_lower_bound(),
whose certificate ignores IO and proves only a necessary completion time.
No official evaluator is imported or executed by this pure generator.
"""
from collections import defaultdict
import heapq
import math
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "solver"), str(HERE.parent / "探索")]
from common import object_digest
from plan import validate_plan
from partition_candidates import topological_order, contiguous_blocks, block_views


def task_lower_bound(ir, plan, cross_wait=1000, same_wait=100):
    """Weighted Task DAG path, with data edges and adjacent same-core edges.

    Task duration >= max(work of each original pipe, internal compute path).
    Cross-core predecessors impose cross_wait; adjacent Tasks on one core
    impose same_wait. Other same-core data edges impose precedence only.
    Longest path in their union is a plan-specific lower bound, NOT a bound
    for another partition/assignment and NOT an achievable schedule.
    """
    if any(type(w) is not int or w < 0 for w in (cross_wait, same_wait)):
        raise ValueError("nonnegative integer Task waits required")
    validate_plan(ir, plan)
    mapping = {int(op): sg for op, sg in plan["node_to_subgraph"].items()}
    core = {sg: n for n, order in enumerate(plan["core_schedules"]) for sg in order}
    work = {sg: defaultdict(int) for sg in core}
    local_end, duration = {}, {sg: 0 for sg in core}
    producers, consumers = defaultdict(list), defaultdict(list)
    for edge in ir.graph["edges"]:
        a, b = edge["source"], edge["target"]
        if a in mapping:
            producers[b].append(a)
        if b in mapping:
            consumers[a].append(b)
    direct_preds = defaultdict(set)
    for tensor, targets in consumers.items():
        for target in targets:
            direct_preds[target].update(producers[tensor])
    for op in topological_order(ir, "stable_id"):
        sg = mapping[op]
        cost = max(1, ir.ops[op]["cycles"])
        work[sg][ir.ops[op]["pipe"]] += cost
        # The P1 compiler deletes original COPY ops and recreates tensor
        # boundaries. A contracted internal COPY chain is not necessarily
        # reinstated inside a Task, so only direct surviving tensor edges
        # may strengthen this local path certificate.
        local_end[op] = cost + max((local_end[p] for p in direct_preds[op]
                                   if mapping[p] == sg), default=0)
        duration[sg] = max(duration[sg], local_end[op])
    for sg in duration:
        duration[sg] = max(duration[sg], max(work[sg].values(), default=0))
    edges = {sg: {} for sg in core}
    for op, successors in ir.successors.items():
        for nxt in successors:
            a, b = mapping[op], mapping[nxt]
            if a != b:
                edges[a][b] = max(edges[a].get(b, 0), cross_wait if core[a] != core[b] else 0)
    for order in plan["core_schedules"]:
        for a, b in zip(order, order[1:]):
            edges[a][b] = max(edges[a].get(b, 0), same_wait)
    degree = {sg: 0 for sg in core}
    preds = {sg: [] for sg in core}
    for a in edges:
        for b, wait in edges[a].items():
            degree[b] += 1
            preds[b].append((a, wait))
    ready = [sg for sg in core if not degree[sg]]
    heapq.heapify(ready)
    ends, witnesses = {}, {}
    while ready:
        sg = heapq.heappop(ready)
        previous = max(preds[sg], key=lambda p: (ends[p[0]] + p[1], -p[0]), default=None)
        ends[sg] = duration[sg] + (ends[previous[0]] + previous[1] if previous else 0)
        witnesses[sg] = previous
        for child in edges[sg]:
            degree[child] -= 1
            if not degree[child]:
                heapq.heappush(ready, child)
    if len(ends) != len(core):
        raise ValueError("Task certificate graph is cyclic")
    path, cursor = [], max(ends, key=lambda sg: (ends[sg], -sg), default=None)
    while cursor is not None:
        pred = witnesses[cursor]
        path.append({"task": cursor, "duration_bound": duration[cursor],
                     "incoming_wait": pred[1] if pred else 0})
        cursor = pred[0] if pred else None
    return {"value": max(ends.values(), default=0), "critical_task_path": path[::-1],
            "task_duration_bounds": duration,
            "scope": "P1 only; fixed plan; original compute and mandatory Task waits; IO omitted"}


def _toposort_blocks(ir, blocks, original_order):
    """Canonical topological block IDs; verify contraction instead of assuming."""
    membership = {op: bid for bid, block in enumerate(blocks) for op in block}
    if len(membership) != len(ir.compute_ids) or sum(map(len, blocks)) != len(membership):
        raise ValueError("partition coverage or duplicate error")
    edges, indeg = [set() for _ in blocks], [0] * len(blocks)
    for op, children in ir.successors.items():
        a = membership[op]
        for child in children:
            b = membership[child]
            if a != b and b not in edges[a]:
                edges[a].add(b)
                indeg[b] += 1
    ready = [bid for bid, d in enumerate(indeg) if not d]
    heapq.heapify(ready)
    order = []
    while ready:
        bid = heapq.heappop(ready)
        order.append(bid)
        for child in sorted(edges[bid]):
            indeg[child] -= 1
            if not indeg[child]:
                heapq.heappush(ready, child)
    if len(order) != len(blocks):
        raise ValueError("partition quotient cycle")
    rank = {op: i for i, op in enumerate(original_order)}
    return [sorted(blocks[bid], key=rank.__getitem__) for bid in order]


def _phase_blocks(ir, nodes, order, bands):
    """Earliest-compute-start bands, then induced WCCs within each band.

    Band indices never decrease along an original edge (positive op costs).
    Within a band different induced WCCs have no edges, so contraction is DAG.
    Splitting at time bands can expose parallel branches that a global work
    quantile would merge. Bands are a partition proxy, not simulated time.
    """
    nodes = set(nodes)
    starts, ends = {}, {}
    for op in order:
        starts[op] = max((ends[p] for p in ir.predecessors[op] if p in nodes), default=0)
        ends[op] = starts[op] + max(1, ir.ops[op]["cycles"])
    length = max(ends.values(), default=1)
    band = {op: min(bands - 1, starts[op] * bands // length) for op in order}
    remaining, blocks = set(order), []
    for op in order:
        if op not in remaining:
            continue
        remaining.remove(op)
        stack, block = [op], []
        while stack:
            node = stack.pop()
            block.append(node)
            for nxt in ir.predecessors[node] + ir.successors[node]:
                if nxt in remaining and band[nxt] == band[node]:
                    remaining.remove(nxt)
                    stack.append(nxt)
        blocks.append(block)
    return blocks


def _assign(ir, blocks, view, active, num_cores, method):
    # List schedule on a block DAG. Unlike a strict block-ID walk, a long tail
    # can run before an unrelated short WCC even if its ID is larger.
    succ = [[] for _ in blocks]
    for bid, preds in enumerate(view["preds"]):
        for pred in preds:
            succ[pred].append(bid)
    durations = []
    for bid in range(len(blocks)):
        reads = sum(ir.input_sizes[t] for t in view["root_inputs"][bid])
        boundary = sum(size for size, _ in view["incoming"][bid].values())
        durations.append(view["work"][bid][2] + (reads + 2 * boundary) / 60)
    tail = [0.0] * len(blocks)
    for bid in reversed(range(len(blocks))):
        tail[bid] = durations[bid] + max((tail[c] for c in succ[bid]), default=0)
    ready = [(-tail[b], b) for b in range(len(blocks)) if not view["preds"][b]]
    heapq.heapify(ready)
    degree = [len(p) for p in view["preds"]]
    ends, assignment = {}, {}
    available, schedules = [0.0] * active, [[] for _ in range(num_cores)]
    while ready:
        _, bid = heapq.heappop(ready)
        def finish(c):
            release = max((ends[p] + (1000 if assignment[p] != c else 0)
                           for p in view["preds"][bid]), default=0)
            return max(release, available[c] + (100 if schedules[c] else 0)) + durations[bid]
        if method == "eft":
            c = min(range(active), key=lambda c: (finish(c), available[c], c))
        else:
            c = min(range(active), key=lambda c: (available[c], finish(c), c))
        ends[bid], assignment[bid] = finish(c), c
        available[c] = ends[bid]
        schedules[c].append(bid)
        for child in succ[bid]:
            degree[child] -= 1
            if not degree[child]:
                heapq.heappush(ready, (-tail[child], child))
    plan = {"node_to_subgraph": {str(op): bid for bid, block in enumerate(blocks) for op in block},
            "core_schedules": schedules}
    validate_plan(ir, plan)
    return plan, max(ends.values(), default=0)


def generate_selective_candidates(ir, num_cores, max_candidates=18, seed=17):
    if type(num_cores) is not int or not 1 <= num_cores <= 5:
        raise ValueError("1..5 cores required")
    if type(max_candidates) is not int or max_candidates < 0:
        raise ValueError("candidate cap must be nonnegative")
    # Seed only rotates otherwise equal-priority family representatives. It
    # does not read case identity or historical scores.
    if type(seed) is not int:
        raise ValueError("integer seed required")
    if not ir.compute_ids or max_candidates == 0:
        return [], {"generated": 0, "selected": 0}
    target_work = max(ir.total_work_m, ir.total_work_v, 1) / num_cores
    heavy = {c.id for c in ir.components if len(c.nodes) >= 16 and
             max(c.work_m, c.work_v, c.work_other) >= 0.75 * target_work}
    candidates, seen = [], set()
    active_values = sorted({num_cores, max(1, math.ceil(num_cores / 2))}, reverse=True)
    for ordering in ("critical_path", "stable_id"):
        full_order = topological_order(ir, ordering)
        per_component = defaultdict(list)
        for op in full_order:
            per_component[ir.component_by_op[op]].append(op)
        for kind, scale in (("whole", 1), ("local_quantile", 1), ("local_quantile", 2),
                            ("phase_band", 1), ("phase_band", 2)):
            if kind == "whole" and ordering != "critical_path":
                continue
            blocks = []
            split_components = []
            for component in ir.components:
                order = per_component[component.id]
                if component.id not in heavy or kind == "whole":
                    pieces = [order]
                elif kind == "local_quantile":
                    pieces = contiguous_blocks(ir, order, max(2, num_cores * scale))
                else:
                    pieces = _phase_blocks(ir, component.nodes, order, max(2, num_cores * scale))
                blocks.extend(pieces)
                if len(pieces) > 1:
                    split_components.append(component.id)
            blocks = _toposort_blocks(ir, blocks, full_order)
            view = block_views(ir, blocks)
            for active in active_values:
                for method in ("eft", "balanced"):
                    plan, proxy = _assign(ir, blocks, view, active, num_cores, method)
                    signature = object_digest(plan)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    name = f"selective_{kind}_{ordering}_x{scale}_a{active}_{method}"
                    candidates.append({"name": name, "plan": plan, "metadata": {
                        "family": "selective_p1", "partition": kind, "ordering": ordering,
                        "scale": scale, "active_core_cap": active, "assignment": method,
                        "split_component_ids": split_components, "task_count": len(blocks),
                        "proxy_end": proxy, "plan_lower_bound": task_lower_bound(ir, plan),
                        "proxy_scope": "ranking only; compute plus approximate IO; ignores dynamic sharing/spill"}})
    # Round-robin structural families before taking additional variants. This
    # avoids a max_candidates prefix consisting solely of one partition.
    groups = defaultdict(list)
    for value in candidates:
        groups[value["metadata"]["partition"]].append(value)
    for values in groups.values():
        values.sort(key=lambda c: (c["metadata"]["proxy_end"], c["name"]))
    family_order = [k for k in ("whole", "local_quantile", "phase_band") if k in groups]
    # Preserve a whole-WCC control first. Seed rotates new-family order only.
    if len(family_order) == 3 and seed % 2 == 0:
        family_order[1:] = family_order[:0:-1]
    selected = []
    for i in range(max((len(g) for g in groups.values()), default=0)):
        for family in family_order:
            if i < len(groups[family]) and len(selected) < max_candidates:
                selected.append(groups[family][i])
    chosen = {c["name"] for c in selected}
    return selected, {"generated": len(candidates), "selected": len(selected),
                      "heavy_component_ids": sorted(heavy), "seed": seed,
                      "omitted": [{"name": c["name"], "proxy_end": c["metadata"]["proxy_end"],
                                   "bound": c["metadata"]["plan_lower_bound"]["value"]}
                                  for c in candidates if c["name"] not in chosen],
                      "scope": "P1 selective WCC decomposition; no measured performance assumed"}
