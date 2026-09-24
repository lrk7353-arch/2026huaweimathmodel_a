"""Pure exploratory P1 affinity regions; separate from frozen v3/fair curves.

Operation assignments supply grouping labels only. SCC condensation makes the
quotient acyclic; P1 EFT then independently assigns the resulting Tasks to cores.
No official evaluation, historical result lookup or case-ID rule occurs here.
"""
from collections import defaultdict
import heapq
import math
from pathlib import Path
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "solver"), str(HERE.parent / "探索")]
from common import object_digest
from plan import validate_plan
from operation_heft_probe import dependency_views, operation_assignment
from partition_candidates import topological_order, block_views
from p1_selective import _toposort_blocks, _assign, task_lower_bound


AFFINITY_SPECS = (("critical_path", 1.0), ("stable_id", 0.25))
BAND_METRICS = ("compute_start", "dependency_depth")
BAND_MULTIPLIERS = (2, 4, 8)


def _quotient(ir, blocks):
    mapping = {op: b for b, block in enumerate(blocks) for op in block}
    if (set(mapping) != set(ir.compute_ids) or sum(map(len, blocks)) != len(mapping)
            or any(not block for block in blocks)):
        raise ValueError("blocks must cover original non-COPY operations exactly once")
    outgoing = [set() for _ in blocks]
    for op, children in ir.successors.items():
        for child in children:
            a, b = mapping[op], mapping[child]
            if a != b:
                outgoing[a].add(b)
    return mapping, outgoing


def _scc_coarsen(ir, blocks, order):
    """Iterative Kosaraju, then topological IDs; safe even for deep quotients.

    Acyclic quotient implies path-convex regions relative to the original DAG:
    an original path leaving and re-entering a region would create a quotient
    cycle. Merging whole SCCs removes every such cycle, not just its first edge.
    """
    membership, adjacency = _quotient(ir, blocks)
    outgoing = [tuple(sorted(v)) for v in adjacency]
    reverse = [[] for _ in blocks]
    for a, children in enumerate(outgoing):
        for b in children:
            reverse[b].append(a)
    visited, finished = set(), []
    for start in range(len(blocks)):
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(outgoing[start]))]
        while stack:
            node, iterator = stack[-1]
            child = next(iterator, None)
            if child is None:
                finished.append(node)
                stack.pop()
            elif child not in visited:
                visited.add(child)
                stack.append((child, iter(outgoing[child])))
    owners, groups = {}, []
    for start in reversed(finished):
        if start in owners:
            continue
        cid, stack, members = len(groups), [start], []
        owners[start] = cid
        while stack:
            node = stack.pop()
            members.append(node)
            for pred in reverse[node]:
                if pred not in owners:
                    owners[pred] = cid
                    stack.append(pred)
        groups.append(sorted(members))
    merged = [[] for _ in groups]
    for op in order:
        merged[owners[membership[op]]].append(op)
    ordered = _toposort_blocks(ir, merged, order)
    final_membership, final_edges = _quotient(ir, ordered)
    if any(a >= b for a, children in enumerate(final_edges) for b in children):
        raise AssertionError("SCC condensation failed to produce a topological quotient")
    # Explicitly verify every original dependency survives as an internal or
    # correctly directed inter-Task dependency, including COPY-contracted edges.
    dependency_count = 0
    for op, children in ir.successors.items():
        for child in children:
            a, b = final_membership[op], final_membership[child]
            if a != b and b not in final_edges[a]:
                raise AssertionError("original dependency lost during contraction")
            dependency_count += 1
    nontrivial = [g for g in groups if len(g) > 1]
    return ordered, {
        "initial_group_count": len(blocks), "final_task_count": len(ordered),
        "nontrivial_scc_count": len(nontrivial), "groups_eliminated": len(blocks) - len(ordered),
        "group_collapse_fraction": (len(blocks) - len(ordered)) / max(1, len(blocks)),
        "largest_scc_group_count": max(map(len, groups), default=0),
        "largest_scc_original_op_count": max((sum(len(blocks[g]) for g in s) for s in groups), default=0),
        "original_dependencies_verified": dependency_count,
        "merged_group_members": nontrivial,
        "convexity_scope": "acyclic quotient of original COPY-contracted compute DAG; not memory feasibility"}


def _band_groups(ir, order, assignment, heavy, count, metric):
    if metric not in BAND_METRICS or type(count) is not int or count < 1:
        raise ValueError("valid metric and positive band count required")
    starts, ends, depth = {}, {}, {}
    lengths, max_depth = defaultdict(int), defaultdict(int)
    for op in order:
        cid = ir.component_by_op[op]
        starts[op] = max((ends[p] for p in ir.predecessors[op]), default=0)
        ends[op] = starts[op] + max(1, ir.ops[op]["cycles"])
        depth[op] = max((depth[p] + 1 for p in ir.predecessors[op]), default=0)
        lengths[cid] = max(lengths[cid], ends[op])
        max_depth[cid] = max(max_depth[cid], depth[op] + 1)
    labels, band_by_op = {}, {}
    for op in order:
        cid = ir.component_by_op[op]
        if cid not in heavy:
            labels[op] = (cid, "whole")
            continue
        coordinate = starts[op] if metric == "compute_start" else depth[op]
        denominator = lengths[cid] if metric == "compute_start" else max_depth[cid]
        band = min(count - 1, coordinate * count // max(1, denominator))
        band_by_op[op] = band
        labels[op] = (cid, band, assignment[op])
    for op, children in ir.successors.items():
        if op in band_by_op:
            for child in children:
                if band_by_op[op] > band_by_op[child]:
                    raise AssertionError("band label decreases along original dependency")
    grouped = {}
    for op in order:
        grouped.setdefault(labels[op], []).append(op)
    groups = list(grouped.values())
    label_list = list(grouped)
    return groups, {"metric": metric, "requested_bands_per_heavy_component": count,
        "heavy_component_ids": sorted(heavy), "group_labels": [list(x) for x in label_list],
        "nonempty_bands_by_heavy_component": {str(cid): len({band_by_op[o] for o in ir.components[cid].nodes})
                                              for cid in sorted(heavy)},
        "assignment_role": "affinity labels only; final P1 Task cores are recomputed"}


def _partition_diagnostics(ir, blocks, view, durations):
    """Graph-only witnesses; neither width nor work/path predicts true speedup."""
    mapping = view["mapping"]
    level, ends = [], []
    levels = defaultdict(list)
    for bid in range(len(blocks)):
        parents = view["preds"][bid]
        level.append(1 + max((level[p] for p in parents), default=-1))
        ends.append(durations[bid] + max((ends[p] for p in parents), default=0))
        levels[level[-1]].append(bid)
    critical = max(ends, default=0)
    best_wave = max(levels.values(), key=lambda ids: (len(ids), sum(durations[i] for i in ids)), default=[])
    # Equal dependency levels form an antichain. Its members can be ready
    # together under this data DAG, but a concrete core schedule can serialize it.
    top_waves = sorted(levels.values(), key=lambda ids: (-len(ids), -sum(durations[i] for i in ids)))[:3]
    producers, consumers = defaultdict(set), defaultdict(set)
    for edge in ir.graph["edges"]:
        a, b = edge["source"], edge["target"]
        if a in mapping:
            producers[b].add(mapping[a])
        if b in mapping:
            consumers[a].add(mapping[b])
    route_bytes, producer_write_bytes, consumer_read_bytes, cut_tensors = 0, 0, 0, 0
    for tid in ir.tensors:
        sources, targets = producers[tid], consumers[tid]
        routes = {(s, t) for s in sources for t in targets if s != t}
        if routes:
            size = ir.tensors[tid]["size"]
            cut_tensors += 1
            route_bytes += size * len(routes)
            producer_write_bytes += size * len({s for s, _ in routes})
            consumer_read_bytes += size * len({t for _, t in routes})
    by_component = defaultdict(list)
    for bid, block in enumerate(blocks):
        cids = {ir.component_by_op[o] for o in block}
        if len(cids) != 1:
            raise AssertionError("unrelated WCCs were merged")
        by_component[next(iter(cids))].append(bid)
    return {"task_count": len(blocks), "largest_task_ops": max(map(len, blocks), default=0),
        "largest_task_op_fraction": max(map(len, blocks), default=0) / max(1, len(ir.compute_ids)),
        "largest_task_compute_bound": max(durations, default=0),
        "sum_task_compute_bounds": sum(durations), "data_dag_compute_bound_path": critical,
        "envelope_work_over_path": sum(durations) / max(1, critical),
        "max_equal_depth_antichain": len(best_wave),
        "top_antichain_witnesses": [{"task_ids": ids, "task_compute_bounds": [durations[i] for i in ids]}
                                    for ids in top_waves],
        "tasks_per_component": {str(cid): len(ids) for cid, ids in sorted(by_component.items())},
        "cut_tensor_count": cut_tensors, "cross_task_tensor_route_bytes": route_bytes,
        "boundary_producer_write_bytes": producer_write_bytes, "boundary_consumer_read_bytes": consumer_read_bytes,
        "traffic_scope": "direct original compute-tensor edges, before spill; excludes root/final IO and internal COPY-chain attribution",
        "parallelism_scope": "data-DAG antichain and envelope-work/path diagnostics only, not achieved parallel execution"}


def generate_convex_candidates(ir, num_cores, max_candidates=12, seed=17):
    """Fixed bounded family portfolio, exact-plan dedup, no evaluator calls."""
    if type(num_cores) is not int or num_cores not in range(1, 6):
        raise ValueError("1..5 cores required")
    if type(max_candidates) is not int or max_candidates < 0 or type(seed) is not int:
        raise ValueError("nonnegative integer candidate cap and integer seed required")
    diag = {"family": "exploratory_p1_convex_regions", "seed": seed, "official_calls": 0,
            "raw_plan_count": 0, "generated_unique": 0, "selected": 0, "generation_failures": [],
            "pruning_performed": False,
            "pruning_rule_for_future_official_controller": "recompute/use certified plan LB only for P1; prune strictly > verified incumbent makespan, never equality",
            "specifications": {"affinity": [list(s) for s in AFFINITY_SPECS], "band_metrics": list(BAND_METRICS),
                "band_multipliers": list(BAND_MULTIPLIERS), "task_core_caps": sorted({num_cores, max(1, math.ceil(num_cores / 2))}, reverse=True)},
            "scope": "pure structure exploration; not integrated into frozen v3 or fair curves"}
    if max_candidates == 0 or not ir.compute_ids:
        return [], diag
    threshold = .75 * max(ir.total_work_m, ir.total_work_v, 1) / num_cores
    heavy = {c.id for c in ir.components if len(c.nodes) >= 16 and max(c.work_m, c.work_v, c.work_other) >= threshold}
    diag["heavy_component_ids"] = sorted(heavy)
    diag["heavy_threshold_work"] = threshold
    try:
        views = dependency_views(ir)
    except ValueError as error:
        diag["generation_failures"].append({"stage": "affinity_tensor_attribution", "error": str(error)})
        return [], diag
    candidates, seen, aliases = [], {}, []
    for ordering, weight in AFFINITY_SPECS:
        order = topological_order(ir, ordering)
        try:
            affinity, affinity_proxy = operation_assignment(ir, order, weight, views, num_cores=num_cores)
        except ValueError as error:
            diag["generation_failures"].append({"stage": "affinity_assignment", "ordering": ordering, "error": str(error)})
            continue
        for metric in BAND_METRICS:
            for multiplier in BAND_MULTIPLIERS:
                initial, band_diag = _band_groups(ir, order, affinity, heavy, multiplier * num_cores, metric)
                blocks, scc = _scc_coarsen(ir, initial, order)
                # A monotone band edge cannot belong to a cycle that returns to
                # a lower band: every merged SCC stays in one (WCC, band).
                for members in scc["merged_group_members"]:
                    labels = [band_diag["group_labels"][i] for i in members]
                    if len({tuple(label[:2]) for label in labels}) != 1:
                        raise AssertionError("SCC unexpectedly crossed a monotone band")
                view = block_views(ir, blocks)
                for active in diag["specifications"]["task_core_caps"]:
                    plan, proxy = _assign(ir, blocks, view, active, num_cores, "eft")
                    validate_plan(ir, plan)
                    lb = task_lower_bound(ir, plan)
                    structure = _partition_diagnostics(ir, blocks, view, [lb["task_duration_bounds"][i] for i in range(len(blocks))])
                    final_core = {sg: c for c, tasks in enumerate(plan["core_schedules"]) for sg in tasks}
                    name = "convex_{}_w{}_{}_b{}_taska{}".format(ordering, round(weight * 100), metric, multiplier, active)
                    metadata = {"family": "p1_convex_regions", "ordering": ordering, "affinity_weight": weight,
                        "band_metric": metric, "band_multiplier": multiplier, "task_active_core_cap": active,
                        "seed": seed, "affinity_assignment_proxy": affinity_proxy,
                        "band_construction": band_diag, "scc": scc, "structure": structure,
                        "plan_lower_bound": lb, "task_proxy_end": proxy,
                        "proxy_scope": "P1 EFT with approximate boundary IO; grouping affinity uses a P2-like 500-delay proxy only as labels; no simulator, DDR contention, capacity/spill or measured timing",
                        "assignment_changed_from_affinity_ops": sum(final_core[plan["node_to_subgraph"][str(o)]] != affinity[o]
                                                                   for o in ir.compute_ids),
                        "also_generated_as": []}
                    signature = object_digest(plan)
                    diag["raw_plan_count"] += 1
                    if signature in seen:
                        seen[signature]["metadata"]["also_generated_as"].append(name)
                        aliases.append({"name": name, "same_as": seen[signature]["name"]})
                    else:
                        row = {"name": name, "plan": plan, "metadata": metadata}
                        candidates.append(row)
                        seen[signature] = row
    # Band metric/scale diversity precedes within-family proxy sorting. Seed
    # rotates the metric starting family, never based on data IDs or scores.
    families = [(m, b) for m in BAND_METRICS for b in BAND_MULTIPLIERS]
    if seed % 2 == 0:
        families = families[3:] + families[:3]
    groups = defaultdict(list)
    for row in candidates:
        key = row["metadata"]["band_metric"], row["metadata"]["band_multiplier"]
        groups[key].append(row)
    for values in groups.values():
        values.sort(key=lambda r: (r["metadata"]["task_proxy_end"], r["metadata"]["plan_lower_bound"]["value"], r["name"]))
    selected = []
    for index in range(max((len(v) for v in groups.values()), default=0)):
        for family in families:
            if index < len(groups[family]) and len(selected) < max_candidates:
                selected.append(groups[family][index])
    chosen = {r["name"] for r in selected}
    diag.update(generated_unique=len(candidates), selected=len(selected), aliases=aliases,
                omitted=[{"name": r["name"], "task_count": r["metadata"]["structure"]["task_count"],
                          "proxy_end": r["metadata"]["task_proxy_end"], "bound": r["metadata"]["plan_lower_bound"]["value"]}
                         for r in candidates if r["name"] not in chosen],
                selection="round-robin metric/band-count families; within family P1 proxy then lower bound/name; exact-plan dedup")
    return selected, diag
