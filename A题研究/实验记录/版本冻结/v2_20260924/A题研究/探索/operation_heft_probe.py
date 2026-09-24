#!/usr/bin/env python3
"""Bounded operation-level HEFT-like probe: four cases, P2 only, <=6 plans each.

No official code/input/config changes. A 180-second evaluation-phase budget
enforces stopping; candidate generation precedes this timer. No second search.
"""
import argparse
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SOLVER = HERE.parent / "solver"
sys.path.insert(0, str(SOLVER))
from graph_ir import GraphIR
from plan import validate_plan
from common import DATA, OFFICIAL, atomic_json, digest, object_digest
from evaluator import evaluate
from partition_candidates import topological_order


def dependency_views(ir):
    producers, consumers = defaultdict(set), defaultdict(set)
    for edge in ir.graph["edges"]:
        a, b = edge["source"], edge["target"]
        if a in ir.ops:
            producers[b].add(a)
        else:
            consumers[a].add(b)
    eligible = set(ir.compute_ids)
    tensors_by_edge = defaultdict(dict)
    for tid, tensor in ir.tensors.items():
        for source in producers[tid] & eligible:
            for target in consumers[tid] & eligible:
                tensors_by_edge[source, target][tid] = tensor["size"]
    all_succ = {op: set() for op in ir.ops}
    for tid in ir.tensors:
        for source in producers[tid]:
            all_succ[source].update(consumers[tid])
    root_inputs = {op: set() for op in ir.compute_ids}
    for tid in ir.input_sizes:
        stack, seen = list(consumers[tid]), set()
        while stack:
            op = stack.pop()
            if op in seen:
                continue
            seen.add(op)
            if op in eligible:
                root_inputs[op].add(tid)
            else:
                stack.extend(all_succ[op])
    unmapped = [(p, op) for op in ir.compute_ids for p in ir.predecessors[op]
                if (p, op) not in tensors_by_edge]
    # All four supplied graphs have direct compute-to-tensor dependencies.
    # Do not silently estimate zero-byte transfer for a new unknown graph shape.
    if unmapped:
        raise ValueError("communication proxy lacks tensor attribution for {} edges".format(len(unmapped)))
    return tensors_by_edge, root_inputs


def operation_assignment(ir, order, communication_weight, views, num_cores=5):
    """EFT on M/V availability + predecessor readiness + COPY-pair estimate.

    For an unseen cross-core tensor route: producer finish + weight *
    (500 + 2*ceil(bytes/60)), lower-bounded at one cycle per COPY.
    Inputs are deduplicated per core and serialized on a proxy read cursor.
    These are placement heuristics: the official 500/60 settings stay fixed.
    """
    edge_tensors, root_inputs = views
    pipe_end = [{"PIPE_M": 0.0, "PIPE_V": 0.0} for _ in range(num_cores)]
    input_cursor = [0.0] * num_cores
    input_ready, transfer_ready = {}, {}
    assignment, finish = {}, {}
    copied_bytes = 0
    for op in order:
        node = ir.ops[op]
        if node["pipe"] not in ("PIPE_M", "PIPE_V"):
            raise ValueError("this M/V probe does not support other compute pipes")
        options = []
        for core in range(num_cores):
            ready = pipe_end[core][node["pipe"]]
            new_transfers, new_inputs = {}, {}
            next_input_cursor = input_cursor[core]
            new_bytes = 0
            for tid in sorted(root_inputs[op]):
                key = (tid, core)
                if key in input_ready:
                    ready = max(ready, input_ready[key])
                else:
                    next_input_cursor += communication_weight * max(1, math.ceil(ir.input_sizes[tid] / 60))
                    new_inputs[key] = next_input_cursor
                    new_bytes += ir.input_sizes[tid]
                    ready = max(ready, next_input_cursor)
            for pred in ir.predecessors[op]:
                source = assignment[pred]
                if source == core:
                    ready = max(ready, finish[pred])
                    continue
                for tid, size in sorted(edge_tensors[pred, op].items()):
                    key = (tid, source, core)
                    if key in transfer_ready:
                        release = transfer_ready[key]
                    elif key in new_transfers:
                        release = new_transfers[key]
                    else:
                        release = finish[pred] + communication_weight * (
                            500 + 2 * max(1, math.ceil(size / 60)))
                        new_transfers[key] = release
                        new_bytes += 2 * size
                    ready = max(ready, release)
            end = ready + max(1, node["cycles"])
            # Traffic tie-break is deliberate even for optimistic weight=0.
            key = (end, new_bytes, pipe_end[core][node["pipe"]], core)
            options.append((key, core, end, next_input_cursor, new_inputs, new_transfers, new_bytes))
        _, core, end, next_cursor, roots, transfers, new_bytes = min(options, key=lambda x: x[0])
        assignment[op], finish[op] = core, end
        pipe_end[core][node["pipe"]] = end
        input_cursor[core] = next_cursor
        input_ready.update(roots)
        transfer_ready.update(transfers)
        copied_bytes += new_bytes
    return assignment, {
        "proxy_makespan_not_official": max(finish.values(), default=0),
        "proxy_pipe_end": pipe_end, "proxy_original_input_and_cross_copy_bytes": copied_bytes,
        "proxy_transfer_routes": len(transfer_ready),
        "ignored_by_proxy": ["dynamic shared DDR contention", "MTE2/MTE3 contention for cross-copy routes",
                             "capacity/spill/memory reuse dependencies", "fixed local Pipe FIFO",
                             "final output traffic", "L2 (P2 has none)"]}


def runs_plan(ir, order, assignment, num_cores=5):
    """Run-length compression in GLOBAL topology, not separate core histories."""
    mapping, schedules = {}, [[] for _ in range(num_cores)]
    previous_core, sg = None, -1
    for op in order:
        core = assignment[op]
        if core != previous_core:
            sg += 1
            schedules[core].append(sg)
            previous_core = core
        mapping[str(op)] = sg
    plan = {"node_to_subgraph": mapping, "core_schedules": schedules}
    validate_plan(ir, plan)
    return plan


def generate_heft_candidates(ir):
    views = dependency_views(ir)
    result, seen = [], {}
    for ordering in ("critical_path", "stable_id"):
        order = topological_order(ir, ordering)
        for weight in (0.0, 0.25, 1.0):
            assignment, proxy = operation_assignment(ir, order, weight, views)
            plan = runs_plan(ir, order, assignment)
            metadata = {"ordering": ordering, "communication_proxy_weight": weight,
                        "official_cross_copy_delay": 500, "official_ddr_bandwidth": 60,
                        "active_cores": sum(bool(s) for s in plan["core_schedules"]),
                        "subgraphs": len(set(plan["node_to_subgraph"].values())), **proxy}
            signature = object_digest(plan)
            if signature in seen:
                seen[signature]["metadata"]["also_generated_as"].append(metadata)
                continue
            name = "{}_comm{:03d}".format(ordering, int(weight * 100))
            metadata["also_generated_as"] = []
            record = {"name": name, "plan": plan, "metadata": metadata}
            seen[signature] = record
            result.append(record)
    assert len(result) <= 6
    return result


def checked_control(ir, entry):
    record = entry["record"]
    if record["status"] != "success":
        raise ValueError("control is not successful")
    hashes = record["hashes"]
    if hashes["graph_sha256"] != digest(ir.path) or hashes["config_sha256"] != digest(DATA / "config.txt"):
        raise ValueError("control input/config hash mismatch")
    if hashes["official_py_sha256"] != {p.name: digest(p) for p in sorted(OFFICIAL.glob("*.py"))}:
        raise ValueError("control official source hash mismatch")
    if digest(record["result_path"]) != record["result_sha256"]:
        raise ValueError("control official result was changed")
    return entry


def write_outputs(out, cases, attempts, budget):
    atomic_json(out / "summary.json", {"scope": "Bounded four-case P2 exploration, not 100-case result",
                                      "evaluation_budget": budget, "cases": cases, "attempts": attempts})
    fields = ["case", "candidate", "status", "makespan", "added_copy_bytes", "elapsed_seconds", "error", "record_path"]
    with (out / "attempts.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(attempts)
    lines = ["# 操作级HEFT式分核：有界探索", "", "仅指定四图、5核、P2；两顺序×三通信代理权重，每图至多6份计划。全部尝试及失败保留。",
             "180秒评估阶段总预算、每次最多45秒、单worker；生成全部候选后开始计评估预算，图顺序051、071、064、049。",
             "对照直接引用已核对图、配置、官方代码及原始结果哈希的先前成功记录，没有重新调优。", "",
             "|用例|整WCC对照|连续块最好|本轮可行最好|允许保留对照后|本轮状态|", "|---|---:|---:|---:|---:|---|"]
    for row in cases:
        lines.append("|{}|{}|{}|{}|{}|{}|".format(row["case"], row["pilot_makespan"], row["partition_makespan"],
                    row.get("best_new_makespan"), row["selected_makespan"], row["status_counts"]))
    lines += ["", "通信代理使用固定官方500周期和60 bytes/cycle，0/0.25/1仅缩放启发式代价，不修改真实评估参数。",
              "代理忽略共享DDR争用、跨核MTE占用、spill/内存复用、编译后FIFO、最终输出搬运。结构合法不保证编译后的执行图无环。",
              "一个核心的连续run指全局拓扑序中的连续区间；不是把同核所有操作强行并成一个子图。",
              "049整WCC对照来自预算截断的pilot，不能称为该类方法的完整最优。"]
    (out / "摘要.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=HERE / "runs" / "operation_heft_v1")
    parser.add_argument("--evaluation-budget", type=float, default=180)
    parser.add_argument("--generate-only", action="store_true")
    args = parser.parse_args(argv)
    if args.evaluation_budget <= 0 or args.evaluation_budget > 180:
        parser.error("evaluation budget must be in (0,180]")
    out = args.run_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    cases_order = ["case_051", "case_071", "case_064", "case_049"]
    prior = json.loads((HERE / "runs/partition_v1/summary.json").read_text())
    prior = {r["case"]: r for r in prior["problems"] if r["problem"] == 2}
    datasets = []
    generation_started = time.perf_counter()
    for case in cases_order:
        ir = GraphIR.from_path(DATA / (case + ".json"))
        candidates = generate_heft_candidates(ir)
        pilot_path = SOLVER / "runs/pilot_v1/results" / case / "simple_p2_n5_seed0.json"
        pilot = json.loads(pilot_path.read_text())
        pilot_best = checked_control(ir, pilot["best"])
        partition_best = checked_control(ir, prior[case]["best"])
        for candidate in candidates:
            atomic_json(out / case / "plans" / (candidate["name"] + ".json"), candidate["plan"])
        atomic_json(out / case / "generation.json", [{"name": c["name"], "metadata": c["metadata"]} for c in candidates])
        atomic_json(out / case / "controls.json", {"pilot": pilot_best, "partition": partition_best,
                    "pilot_source": str(pilot_path), "pilot_stop_reason": pilot["stop_reason"]})
        datasets.append((ir, candidates, pilot_best, partition_best))
    manifest = {"script_sha256": digest(Path(__file__)), "candidate_order": cases_order,
                "source_sha256": {str(p): digest(p) for base in (SOLVER, OFFICIAL) for p in sorted(base.glob("*.py"))},
                "partition_generator_sha256": digest(HERE / "partition_candidates.py"),
                "config_sha256": digest(DATA / "config.txt"), "generation_seconds": time.perf_counter() - generation_started,
                "problem": 2, "cores": 5, "communication_proxy_weights": [0, 0.25, 1],
                "single_call_timeout_cap": 45, "evaluation_budget_seconds": args.evaluation_budget,
                "worker_concurrency": 1}
    atomic_json(out / "manifest.json", manifest)
    if args.generate_only:
        print(json.dumps({"generated": {ir.path.stem: len(c) for ir, c, _, _ in datasets},
                          "generation_seconds": manifest["generation_seconds"]}), flush=True)
        return 0
    started = time.monotonic()
    deadline = started + args.evaluation_budget
    cases, attempts = [], []
    exhausted = False
    for ir, candidates, pilot, partition in datasets:
        results, best_new = [], None
        selected = partition
        for candidate in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                exhausted = True
                record = {"status": "not_evaluated_budget", "metrics": {}, "elapsed_seconds": 0.0,
                          "error": "180-second evaluation-phase budget exhausted", "record_path": None}
            else:
                record = evaluate(ir.path, candidate["plan"], 2, out / "evaluations", timeout=min(45, remaining))
            row = {"candidate": candidate["name"], "metadata": candidate["metadata"], "record": record}
            results.append(row)
            if record["status"] == "success":
                enriched = {**row, "plan": candidate["plan"]}
                if best_new is None or record["metrics"]["makespan"] < best_new["record"]["metrics"]["makespan"]:
                    best_new = enriched
                if record["metrics"]["makespan"] < selected["record"]["metrics"]["makespan"]:
                    selected = enriched
            metrics = record.get("metrics", {})
            attempts.append({"case": ir.path.stem, "candidate": candidate["name"], "status": record["status"],
                             "makespan": metrics.get("makespan"),
                             "added_copy_bytes": metrics.get("data_movement_bytes", {}).get("added_copy_bytes"),
                             "elapsed_seconds": record["elapsed_seconds"], "error": record.get("error"),
                             "record_path": record.get("record_path")})
            atomic_json(out / ir.path.stem / "all_evaluations.json", results)
            print(json.dumps({"case": ir.path.stem, "candidate": candidate["name"],
                              "status": record["status"], "makespan": metrics.get("makespan")}), flush=True)
        summary = {"case": ir.path.stem, "pilot_makespan": pilot["record"]["metrics"]["makespan"],
                   "partition_makespan": partition["record"]["metrics"]["makespan"],
                   "best_new_makespan": best_new["record"]["metrics"]["makespan"] if best_new else None,
                   "selected_makespan": selected["record"]["metrics"]["makespan"],
                   "selected": selected, "best_new": best_new,
                   "status_counts": dict(Counter(r["record"]["status"] for r in results))}
        cases.append(summary)
        atomic_json(out / ir.path.stem / "selected.plan.json", selected["plan"])
        write_outputs(out, cases, attempts, {"limit_seconds": args.evaluation_budget,
                                            "elapsed_seconds": time.monotonic() - started, "exhausted": exhausted})
    atomic_json(out / "completion.json", {"elapsed_evaluation_phase_seconds": time.monotonic() - started,
                "completed": True, "budget_exhausted": exhausted,
                "status_counts": dict(Counter(r["status"] for r in attempts)), "rows": len(attempts)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
