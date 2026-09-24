#!/usr/bin/env python3
"""Independent, read-only audit of A graphs. Never imports or executes evaluators."""
import argparse
import csv
import hashlib
import heapq
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

COPY_TYPES = {"COPY_IN", "COPY_OUT"}


class DSU:
    def __init__(self, ids):
        self.parent = {i: i for i in ids}
        self.size = {i: 1 for i in ids}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a == b:
            return
        if self.size[a] < self.size[b]:
            a, b = b, a
        self.parent[b] = a
        self.size[a] += self.size[b]

    def groups(self):
        groups = defaultdict(list)
        for x in self.parent:
            groups[self.find(x)].append(x)
        return sorted((sorted(g) for g in groups.values()), key=lambda g: (-len(g), g[0]))


def audit(path):
    raw = path.read_bytes()
    graph = json.loads(raw)
    ops = {x["id"]: x for x in graph["ops"]}
    tensors = {x["id"]: x for x in graph["tensors"]}
    eligible = {i for i, op in ops.items() if op["op"] not in COPY_TYPES}
    producers, consumers = defaultdict(set), defaultdict(set)
    op_inputs, op_outputs = defaultdict(set), defaultdict(set)
    full_succ = {i: set() for i in ops}
    defects = Counter()
    defects["duplicate_op_ids"] = len(graph["ops"]) - len(ops)
    defects["duplicate_tensor_ids"] = len(graph["tensors"]) - len(tensors)
    defects["op_tensor_id_overlap"] = len(set(ops) & set(tensors))
    seen_edges = set()
    for e in graph["edges"]:
        a, b = e["source"], e["target"]
        if (a, b) in seen_edges:
            defects["duplicate_edges"] += 1
        seen_edges.add((a, b))
        if a in ops and b in tensors:
            producers[b].add(a)
            op_outputs[a].add(b)
        elif a in tensors and b in ops:
            consumers[a].add(b)
            op_inputs[b].add(a)
        elif a in ops and b in ops:
            full_succ[a].add(b)
            defects["direct_op_op_edges"] += 1
        else:
            defects["invalid_or_tensor_tensor_edges"] += 1
    for t in tensors:
        for p in producers[t]:
            full_succ[p].update(consumers[t] - {p})
        if len(producers[t]) > 1:
            defects["multi_producer_tensors"] += 1

    # Trace through zero or more COPY operators to nearest non-COPY successors.
    # Sibling consumers of an input do NOT become data-dependent on each other.
    def nearest_compute(starts):
        result, seen_copy, stack = set(), set(), list(starts)
        while stack:
            current = stack.pop()
            if current in eligible:
                result.add(current)
            elif current not in seen_copy:
                seen_copy.add(current)
                stack.extend(full_succ[current])
        return result

    succ = {i: nearest_compute(full_succ[i]) - {i} for i in eligible}
    direct_succ = {i: full_succ[i] & eligible for i in eligible}
    dep_dsu, direct_dsu, touch_dsu = DSU(eligible), DSU(eligible), DSU(eligible)
    for i in eligible:
        for j in succ[i]:
            dep_dsu.union(i, j)
        for j in direct_succ[i]:
            direct_dsu.union(i, j)
    for t, tensor in tensors.items():
        if tensor["pos"] not in {"L1", "UB"}:
            continue
        touched = sorted((producers[t] | consumers[t]) & eligible)
        for j in touched[1:]:
            touch_dsu.union(touched[0], j)
    groups = dep_dsu.groups()
    group_id = {op: i for i, g in enumerate(groups) for op in g}
    touch_groups = touch_dsu.groups()
    touch_id = {op: i for i, g in enumerate(touch_groups) for op in g}

    # Original graph inputs = producer-free DDR tensors consumed by COPY_IN.
    copy_in = [i for i, op in ops.items() if op["op"] == "COPY_IN"]
    copy_out = [i for i, op in ops.items() if op["op"] == "COPY_OUT"]
    input_tensors = sorted(t for t, v in tensors.items() if v["pos"] == "DDR"
                           and not producers[t]
                           and any(ops[i]["op"] == "COPY_IN" for i in consumers[t]))
    sharing_rows = []
    for t in input_tensors:
        reach = nearest_compute({i for i in consumers[t] if ops[i]["op"] == "COPY_IN"})
        comps = sorted({group_id[i] for i in reach})
        touch_comps = sorted({touch_id[i] for i in reach})
        sharing_rows.append({"case": path.stem, "tensor_id": t, "bytes": tensors[t]["size"],
                             "first_compute_consumers": len(reach),
                             "dependency_components": len(comps),
                             "touch_components": len(touch_comps),
                             "component_ids": ";".join(map(str, comps))})
    for i in copy_in:
        if len(op_inputs[i]) != 1 or len(op_outputs[i]) != 1:
            defects["copy_in_not_one_input_one_output"] += 1
        if any(producers[t] for t in op_inputs[i] if tensors[t]["pos"] == "DDR"):
            defects["copy_in_from_nonroot_ddr"] += 1
    for t in input_tensors:
        if sum(ops[i]["op"] == "COPY_IN" for i in consumers[t]) != 1:
            defects["original_input_not_copied_once"] += 1
    input_bytes = sum(sum(tensors[t]["size"] for t in op_outputs[i]) for i in copy_in)
    output_bytes = sum(sum(tensors[t]["size"] for t in op_inputs[i]) for i in copy_out)

    # Weighted CP includes non-COPY computation only; no transfer/sync/spill time.
    indegree = {i: 0 for i in eligible}
    for i in eligible:
        for j in succ[i]:
            indegree[j] += 1
    roots = sum(d == 0 for d in indegree.values())
    ready = [i for i in eligible if indegree[i] == 0]
    heapq.heapify(ready)
    depth = {i: 1 for i in eligible}
    weight = {i: max(1, ops[i]["cycles"]) for i in eligible}
    cp = dict(weight)
    visited = 0
    while ready:
        i = heapq.heappop(ready)
        visited += 1
        for j in succ[i]:
            depth[j] = max(depth[j], depth[i] + 1)
            cp[j] = max(cp[j], cp[i] + weight[j])
            indegree[j] -= 1
            if indegree[j] == 0:
                heapq.heappush(ready, j)
    if visited != len(eligible):
        raise ValueError(f"{path.name}: contracted compute dependencies contain a cycle")
    layers = Counter(depth.values())
    work = Counter()
    op_counts = Counter()
    for i in eligible:
        work[ops[i]["pipe"]] += weight[i]
        op_counts[ops[i]["pipe"]] += 1
    component_rows = []
    for cid, nodes in enumerate(groups):
        wm = sum(weight[i] for i in nodes if ops[i]["pipe"] == "PIPE_M")
        wv = sum(weight[i] for i in nodes if ops[i]["pipe"] == "PIPE_V")
        component_rows.append({"case": path.stem, "component_id": cid, "ops": len(nodes),
                               "fraction_ops": len(nodes) / len(eligible), "work_M": wm,
                               "work_V": wv, "total_compute_work": sum(weight[i] for i in nodes),
                               "compute_critical_path": max(cp[i] for i in nodes),
                               "dependency_layers": max(depth[i] for i in nodes), "minimum_op_id": min(nodes)})
    lb_terms = {"PIPE_M": work["PIPE_M"] / 5, "PIPE_V": work["PIPE_V"] / 5,
                "original_DDR": (input_bytes + output_bytes) / 60,
                "compute_CP": max(cp.values(), default=0)}
    max_lb = max(lb_terms.values())
    winners = [k for k, v in lb_terms.items() if abs(v - max_lb) < 1e-9]
    atomic_lb = max(max_lb, max(x["work_M"] for x in component_rows),
                    max(x["work_V"] for x in component_rows))
    row = {"case": path.stem, "compute_ops": len(eligible), "all_ops": len(ops),
           "tensors": len(tensors), "edges": len(graph["edges"]),
           "compute_dependency_edges": sum(map(len, succ.values())),
           "copy_contracted_extra_edges": sum(len(succ[i] - direct_succ[i]) for i in eligible),
           "dependency_components": len(groups), "direct_dependency_components": len(direct_dsu.groups()),
           "touch_components": len(touch_groups), "largest_component_ops": len(groups[0]),
           "largest_component_fraction": len(groups[0]) / len(eligible),
           "largest_touch_component_fraction": len(touch_groups[0]) / len(eligible),
           "singleton_components": sum(len(g) == 1 for g in groups),
           "largest_component_work_M_fraction": max(x["work_M"] for x in component_rows) / work["PIPE_M"] if work["PIPE_M"] else 0,
           "largest_component_work_V_fraction": max(x["work_V"] for x in component_rows) / work["PIPE_V"] if work["PIPE_V"] else 0,
           "dependency_layers": max(depth.values(), default=0), "max_layer_width": max(layers.values(), default=0),
           "root_compute_ops": roots, "sink_compute_ops": sum(not s for s in succ.values()),
           "work_M": work["PIPE_M"], "work_V": work["PIPE_V"],
           "count_M": op_counts["PIPE_M"], "count_V": op_counts["PIPE_V"],
           "noncopy_other_pipe_ops": sum(op_counts[p] for p in op_counts if p not in {"PIPE_M", "PIPE_V"}),
           "compute_critical_path": max(cp.values(), default=0), "original_copy_in_bytes": input_bytes,
           "original_copy_out_bytes": output_bytes, "root_ddr_input_unique_bytes": sum(tensors[t]["size"] for t in input_tensors),
           "root_ddr_input_count": len(input_tensors),
           "max_root_ddr_input_bytes": max((tensors[t]["size"] for t in input_tensors), default=0),
           "max_all_tensor_bytes": max(x["size"] for x in tensors.values()),
           "shared_ddr_inputs_across_components": sum(x["dependency_components"] > 1 for x in sharing_rows),
           "shared_ddr_inputs_across_touch_components": sum(x["touch_components"] > 1 for x in sharing_rows),
           "max_input_component_fanout": max((x["dependency_components"] for x in sharing_rows), default=0),
           "five_core_baseline_lb_M": lb_terms["PIPE_M"], "five_core_baseline_lb_V": lb_terms["PIPE_V"],
           "five_core_baseline_lb_DDR": lb_terms["original_DDR"],
           "five_core_baseline_lb_CP": lb_terms["compute_CP"],
           "five_core_whole_component_lb": atomic_lb,
           "whole_component_lb_over_baseline_lb": atomic_lb / max_lb if max_lb else 1,
           "five_core_baseline_lb_dominant": ";".join(winners)}
    return row, component_rows, sharing_rows, {k: v for k, v in defects.items() if v}, hashlib.sha256(raw).hexdigest()


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = sorted(args.data.glob("case_[0-9][0-9][0-9].json"))
    rows, components, sharing, defects, hashes = [], [], [], {}, {}
    for path in cases:
        row, comp, share, bad, sha = audit(path)
        rows.append(row)
        components.extend(comp)
        sharing.extend(share)
        if bad:
            defects[path.stem] = bad
        hashes[path.name] = sha
    args.output.mkdir(parents=True, exist_ok=True)
    write_csv(args.output / "a_graph_structure.csv", rows)
    write_csv(args.output / "a_dependency_components.csv", components)
    write_csv(args.output / "a_input_sharing.csv", sharing)
    fields = ["compute_ops", "tensors", "edges", "dependency_components", "largest_component_fraction",
              "dependency_layers", "max_layer_width", "work_M", "work_V", "compute_critical_path",
              "original_copy_in_bytes", "max_root_ddr_input_bytes"]
    stats = {k: {"min": min(r[k] for r in rows), "median": statistics.median(r[k] for r in rows),
                 "max": max(r[k] for r in rows)} for k in fields}
    summary = {"case_count": len(rows), "statistics": stats,
               "single_dependency_component_cases": sum(r["dependency_components"] == 1 for r in rows),
               "multiple_dependency_component_cases": sum(r["dependency_components"] > 1 for r in rows),
               "sharing_across_dependency_components_cases": sum(r["shared_ddr_inputs_across_components"] > 0 for r in rows),
               "single_touch_component_cases": sum(r["touch_components"] == 1 for r in rows),
               "touch_largest_over_90pct_cases": sum(r["largest_touch_component_fraction"] > 0.9 for r in rows),
               "sharing_across_touch_components_cases": sum(r["shared_ddr_inputs_across_touch_components"] > 0 for r in rows),
               "original_copy_in_at_most_1MiB_cases": sum(r["original_copy_in_bytes"] <= 1048576 for r in rows),
               "original_copy_in_at_most_1MB_decimal_cases": sum(r["original_copy_in_bytes"] <= 1000000 for r in rows),
               "max_all_tensor_bytes": max(r["max_all_tensor_bytes"] for r in rows),
               "copy_contracted_extra_dependency_edges": sum(r["copy_contracted_extra_edges"] for r in rows),
               "five_core_baseline_lb_dominant_counts": dict(Counter(r["five_core_baseline_lb_dominant"] for r in rows)),
               "large_component_over_90pct_cases": sum(r["largest_component_fraction"] > 0.9 for r in rows),
               "large_component_over_50pct_cases": sum(r["largest_component_fraction"] > 0.5 for r in rows),
               "multicomponent_largest_over_50pct_cases": sum(r["dependency_components"] > 1 and r["largest_component_fraction"] > 0.5 for r in rows),
               "multicomponent_largest_over_90pct_cases": sum(r["dependency_components"] > 1 and r["largest_component_fraction"] > 0.9 for r in rows),
               "component_count_bins": {name: sum(lo <= r["dependency_components"] <= hi for r in rows)
                                        for name, lo, hi in [("1", 1, 1), ("2-4", 2, 4), ("5-19", 5, 19),
                                                           ("20-99", 20, 99), ("100-999", 100, 999), ("1000+", 1000, 99999)]},
               "multicomponent_atomic_lb_at_least_1_5x_baseline_cases": sum(r["dependency_components"] > 1 and r["whole_component_lb_over_baseline_lb"] >= 1.5 for r in rows),
               "root_unique_input_bytes_equal_copy_in_bytes_cases": sum(r["root_ddr_input_unique_bytes"] == r["original_copy_in_bytes"] for r in rows),
               "input_schema_anomalies": defects,
               "definitions": {
                   "dependency_component": "Weak component of non-COPY operator DAG. Tensor producer-to-consumer dependency; COPY_IN/COPY_OUT paths are contracted to nearest compute successors. Co-consumers alone are not connected.",
                   "touch_component": "Weak component formed by joining all non-COPY producers and consumers that touch each L1/UB tensor; intentionally different from dependency component.",
                   "original_ddr_input": "Producer-free DDR tensor read by original COPY_IN; consumers are followed through COPY operators until nearest compute operators.",
                   "input_sharing": "One original DDR input reaches nearest compute consumers in at least two dependency components.",
                   "original_copy_in_bytes": "Sum of output tensor sizes of all original COPY_IN operators, same counting convention as supplied evaluator; unique original DDR input bytes reported separately.",
                   "layers": "Maximum count of compute nodes on a directed dependency path; max_layer_width counts nodes with the same longest-path layer and is not exact DAG maximum antichain width.",
                   "critical_path": "Longest compute-only path with duration max(1, cycles); excludes transfer, synchronization and spill; not official makespan.",
                   "five_core_diagnostic": "max(work_M/5, work_V/5, original COPY bytes/60, compute CP). This is a baseline resource lower-bound diagnostic, not a measurement of final simulated bottlenecks.",
                   "whole_component_lb": "If each entire dependency component must stay on one core, makespan is also bounded below by the largest component's PIPE_M workload and by the largest component's PIPE_V workload; compare with unconstrained baseline lower bound. This ratio is not measured speedup or an optimality-gap bound.",
               }, "input_sha256": hashes}
    (args.output / "a_structure_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in {"input_sha256", "definitions"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
