"""Read-only structural audit plus exactly two fresh official evaluations of the selected move."""
from collections import defaultdict
from datetime import datetime, timezone
import heapq
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
sys.path.insert(0, str(ROOT / "A题研究/solver"))
from common import atomic_json, digest, object_digest, read_json
from evaluator import evaluate


def core_assignment(plan):
    owners = {}
    for core, subgraphs in enumerate(plan["core_schedules"]):
        for sg in subgraphs:
            if sg in owners:
                raise ValueError("duplicate scheduled subgraph")
            owners[sg] = core
    if set(owners) != set(plan["node_to_subgraph"].values()):
        raise ValueError("mapping/schedules disagree")
    return {int(op): owners[sg] for op, sg in plan["node_to_subgraph"].items()}


def independent_stable_order(graph):
    ops = {op["id"]: op for op in graph["ops"]}
    compute = {op for op, data in ops.items() if data["op"] not in {"COPY_IN", "COPY_OUT"}}
    producers, consumers = defaultdict(set), defaultdict(set)
    for edge in graph["edges"]:
        a, b = edge["source"], edge["target"]
        if a in ops:
            producers[b].add(a)
        else:
            consumers[a].add(b)
    full = defaultdict(set)
    for tensor, parents in producers.items():
        for parent in parents:
            full[parent].update(consumers[tensor])
    successors = {op: set() for op in compute}
    for parent in compute:
        stack, seen = list(full[parent]), set()
        while stack:
            child = stack.pop()
            if child in seen:
                continue
            seen.add(child)
            if child in compute:
                successors[parent].add(child)
            else:
                stack.extend(full[child])
    indegree = {op: 0 for op in compute}
    for children in successors.values():
        for child in children:
            indegree[child] += 1
    ready = [op for op in compute if not indegree[op]]
    heapq.heapify(ready)
    order = []
    while ready:
        op = heapq.heappop(ready)
        order.append(op)
        for child in successors[op]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, child)
    if len(order) != len(compute):
        raise ValueError("compute dependency cycle")
    return order


def independent_runs_plan(order, assignments, cores):
    mapping, schedules = {}, [[] for _ in range(cores)]
    previous, sg = None, -1
    for op in order:
        core = assignments[op]
        if core != previous:
            sg += 1
            schedules[core].append(sg)
            previous = core
        mapping[str(op)] = sg
    return {"node_to_subgraph": mapping, "core_schedules": schedules}


def main():
    if (OUT / "verification.json").exists():
        raise RuntimeError("Verification already exists; do not overwrite it.")
    oldpath = ROOT / "A题研究/探索/runs/operation_heft_v1/case_071/selected.plan.json"
    newpath = ROOT / "A题研究/探索/runs/critical_transfer_v1/case_071/selected.plan.json"
    graphpath = ROOT / "选题分析/A题附件/data/case_071.json"
    old, new, graph = read_json(oldpath), read_json(newpath), read_json(graphpath)
    old_assignment, new_assignment = core_assignment(old), core_assignment(new)
    order = independent_stable_order(graph)
    changes = [{"op": op, "old_core": old_assignment[op], "new_core": new_assignment[op]}
               for op in sorted(old_assignment) if old_assignment[op] != new_assignment[op]]
    official = read_json(ROOT / "A题研究/方案审阅/cache_microtests/provenance.json")["files_sha256"]
    oldcheck = next(c for c in read_json(ROOT / "A题研究/实验记录/independent_heft_check/verification.json")["cases"] if c["case"] == "case_071")
    report = {"created_utc": datetime.now(timezone.utc).isoformat(), "case": "case_071",
              "workers": 1, "timeout_seconds": 60, "initial_cache_empty": True,
              "old_plan": str(oldpath), "new_plan": str(newpath),
              "old_file_sha256": digest(oldpath), "new_file_sha256": digest(newpath),
              "original_graph_sha256": digest(graphpath), "global_order_sha256": object_digest(order),
              "compute_operations": len(order), "assignment_changes": changes,
              "only_expected_move": changes == [{"op": 720, "old_core": 3, "new_core": 2}],
              "exact_compute_coverage": set(old_assignment) == set(new_assignment) == set(order),
              "same_stable_order_in_mapping": list(map(int, old["node_to_subgraph"])) == list(map(int, new["node_to_subgraph"])) == order,
              "old_exact_runs_encoding": object_digest(old) == object_digest(independent_runs_plan(order, old_assignment, len(old["core_schedules"]))),
              "new_exact_runs_encoding": object_digest(new) == object_digest(independent_runs_plan(order, new_assignment, len(new["core_schedules"]))),
              "old_subgraphs": len(set(old["node_to_subgraph"].values())),
              "new_subgraphs": len(set(new["node_to_subgraph"].values())),
              "official_hash_checks_before": {path: digest(ROOT / path) == expected for path, expected in official.items()},
              "old_independently_verified_p2": oldcheck["evaluations"]["2"]["metrics"],
              "old_independently_verified_p3": oldcheck["evaluations"]["3"]["metrics"], "evaluations": {}}
    atomic_json(OUT / "verification.json", report)
    for problem in (2, 3):
        record = evaluate(graphpath, new, problem, OUT / "evaluations", timeout=60,
                          config_path=ROOT / "选题分析/A题附件/data/config.txt")
        report["evaluations"][str(problem)] = record
        atomic_json(OUT / "verification.json", report)
        print(json.dumps({"problem": problem, "status": record["status"], "cache_hit": record["cache_hit"],
                          "metrics": record.get("metrics")}, ensure_ascii=False), flush=True)
    p2, p3 = report["evaluations"]["2"], report["evaluations"]["3"]
    report["p2_matches_claim"] = (p2["status"] == "success" and p2["metrics"]["makespan"] == 9792
                                   and p2["metrics"]["data_movement_bytes"]["added_copy_bytes"] == 373042)
    report["all_fresh_success"] = all(r["status"] == "success" and not r["cache_hit"] for r in report["evaluations"].values())
    if p3["status"] == "success":
        report["p3_vs_old_cycles"] = p3["metrics"]["makespan"] - report["old_independently_verified_p3"]["makespan"]
        report["recommended_p3_plan"] = str(oldpath if report["p3_vs_old_cycles"] > 0 else newpath)
    else:
        report["recommended_p3_plan"] = str(oldpath)
    report["official_hash_checks_after"] = {path: digest(ROOT / path) == expected for path, expected in official.items()}
    atomic_json(OUT / "verification.json", report)
    return 0 if report["all_fresh_success"] and report["p2_matches_claim"] and report["only_expected_move"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
