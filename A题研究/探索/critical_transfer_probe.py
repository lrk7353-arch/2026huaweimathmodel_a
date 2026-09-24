#!/usr/bin/env python3
"""One bounded P2/N5 neighbourhood from saved HEFT incumbents, not hill climbing.

Late COPY_IN completion is only a candidate-ranking feature, not a recovered
critical path. Keep the incumbent's global compute topology fixed. No official
source/config/input edits. At most 8 unique neighbours/case, 30 seconds/call,
90 seconds for the evaluation phase (generation excluded), one worker.
"""
import argparse
from collections import Counter, defaultdict
import csv
import gzip
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SOLVER = HERE.parent / "solver"
sys.path.insert(0, str(SOLVER))
from common import DATA, OFFICIAL, atomic_json, digest, object_digest, read_json
from evaluator import evaluate
from graph_ir import GraphIR
from plan import validate_plan
from operation_heft_probe import checked_control, runs_plan
from partition_candidates import topological_order

PRIOR = HERE / "runs" / "operation_heft_v1"
CASES = ("case_049", "case_071")  # Experiment scope, never a placement rule.


def assignment_from_plan(ir, plan):
    validate_plan(ir, plan)
    if len(plan["core_schedules"]) != 5:
        raise ValueError("probe requires exactly five hardware core schedules")
    core = {sg: c for c, schedule in enumerate(plan["core_schedules"]) for sg in schedule}
    return {int(op): core[sg] for op, sg in plan["node_to_subgraph"].items()}


def original_tensor_views(ir):
    producers, consumers = defaultdict(set), defaultdict(set)
    for edge in ir.graph["edges"]:
        a, b = edge["source"], edge["target"]
        if a in ir.ops and b in ir.tensors:
            producers[b].add(a)
        elif a in ir.tensors and b in ir.ops:
            consumers[a].add(b)
        else:
            raise ValueError("unsupported original edge; no guessed tensor attribution")
    compute = set(ir.compute_ids)
    return ({t: producers[t] & compute for t in ir.tensors},
            {t: consumers[t] & compute for t in ir.tensors})


def cross_routes(ir, assignment, views):
    producers, consumers = views
    return {(tid, source, target): ir.tensors[tid]["size"]
            for tid in ir.tensors
            for source in {assignment[o] for o in producers[tid]}
            for target in {assignment[o] for o in consumers[tid]}
            if source != target}


def checked_timeline_mapping(ir, official, assignment, views):
    """Require exact original tensor + unique compute producer + timeline joins."""
    producers, consumers = views
    operations = {}
    for core in official["per_core_timeline"]:
        for op in core["ops"]:
            key = core["core_id"], op["op_id"]
            if key in operations:
                raise ValueError("duplicate official timeline operation")
            operations[key] = op
    seen, rows = set(), []
    for link in official["cross_core_transfers"]:
        tid, src, dst = link["tensor_id"], link["source_core"], link["target_core"]
        key = tid, src, dst
        if key in seen or tid not in ir.tensors or len(producers[tid]) != 1:
            raise ValueError("ambiguous or synthetic transfer tensor {}; do not guess".format(key))
        seen.add(key)
        producer = next(iter(producers[tid]))
        targets = sorted(o for o in consumers[tid] if assignment[o] == dst)
        if assignment[producer] != src or not targets or src == dst:
            raise ValueError("original producer/consumer core attribution mismatch")
        out = operations[src, link["source_copy_out_id"]]
        inn = operations[dst, link["target_copy_in_id"]]
        if not (link["size"] == ir.tensors[tid]["size"]
                and out["op"] == "COPY_OUT" and inn["op"] == "COPY_IN"
                and out["end"] == link["copy_out_end"]
                and inn["start"] == link["copy_in_start"]
                and inn["end"] == link["copy_in_end"]
                and link["copy_in_release"] == link["copy_out_end"] + 500
                and link["copy_in_start"] >= link["copy_in_release"]):
            raise ValueError("official transfer/timeline fields disagree")
        for op in [producer] + targets:
            if operations[assignment[op], op]["op"] != ir.ops[op]["op"]:
                raise ValueError("original compute id not preserved in timeline")
        targets.sort(key=lambda o: (-operations[dst, o]["end"], o))
        rows.append({**link, "original_producer": producer,
                     "original_consumers_on_target": targets,
                     "queue_wait_after_release": link["copy_in_start"] - link["copy_in_release"],
                     "distance_to_makespan": official["makespan"] - link["copy_in_end"],
                     "producer_end": operations[src, producer]["end"],
                     "consumer_end_by_op": {str(o): operations[dst, o]["end"] for o in targets}})
    expected = cross_routes(ir, assignment, views)
    if seen != set(expected) or sum(expected.values()) != official["cross_task_traffic"]:
        raise ValueError("saved transfers do not exactly cover original tensor core routes")
    rows.sort(key=lambda r: (-r["copy_in_end"], -r["queue_wait_after_release"],
                            -r["size"], r["tensor_id"], r["source_core"], r["target_core"]))
    for rank, row in enumerate(rows, 1):
        row["candidate_priority_rank"] = rank
    return rows


def short_chain(ir, seed, assignment, backwards, maximum=3):
    """Up to 3 original-core ops on a strict unbranched compute dependency chain."""
    chain, current = [seed], seed
    forward = ir.predecessors if backwards else ir.successors
    reverse = ir.successors if backwards else ir.predecessors
    while len(chain) < maximum and len(forward[current]) == 1:
        other = next(iter(forward[current]))
        if len(reverse[other]) != 1 or assignment[other] != assignment[seed]:
            break
        chain.append(other)
        current = other
    return chain


def prepare(ir):
    source_path = PRIOR / ir.path.stem / "all_evaluations.json"
    prior = read_json(source_path)
    successful = [r for r in prior if r["record"]["status"] == "success"]
    incumbent = min(successful, key=lambda r: (r["record"]["metrics"]["makespan"],
                    r["record"]["metrics"]["data_movement_bytes"]["added_copy_bytes"]))
    checked_control(ir, incumbent)
    record = incumbent["record"]
    if record["problem"] != 2 or digest(record["plan_path"]) != record["hashes"]["plan_sha256"]:
        raise ValueError("incumbent plan hash/problem mismatch")
    plan = read_json(record["plan_path"])
    selected_path = PRIOR / ir.path.stem / "selected.plan.json"
    if object_digest(plan) != object_digest(read_json(selected_path)):
        raise ValueError("saved selected plan differs from best successful incumbent")
    assignment = assignment_from_plan(ir, plan)
    order = topological_order(ir, incumbent["metadata"]["ordering"])
    if object_digest(runs_plan(ir, order, assignment)) != object_digest(plan):
        raise ValueError("incumbent global topology cannot be exactly reconstructed")
    with gzip.open(record["result_path"], "rt", encoding="utf-8") as stream:
        official = json.load(stream)
    if official["makespan"] != record["metrics"]["makespan"] or official["num_cores"] != 5:
        raise ValueError("saved official metrics disagree")
    views = original_tensor_views(ir)
    ranked = checked_timeline_mapping(ir, official, assignment, views)
    baseline_traffic = sum(cross_routes(ir, assignment, views).values())
    candidates, seen, generation_log = [], {object_digest(plan)}, []
    for row in ranked:
        producer, consumer = row["original_producer"], row["original_consumers_on_target"][0]
        options = [
            ("producer_single", [producer], row["target_core"]),
            ("consumer_single", [consumer], row["source_core"]),
            ("producer_chain", short_chain(ir, producer, assignment, True), row["target_core"]),
            ("consumer_chain", short_chain(ir, consumer, assignment, False), row["source_core"]),
        ]
        for strategy, moving, destination in options:
            changed = dict(assignment)
            for op in moving:
                changed[op] = destination
            name = "rank{:03d}_{}".format(row["candidate_priority_rank"], strategy)
            meta = {"strategy": strategy, "seed_transfer": row,
                    "moved_ops": sorted(moving), "destination_core": destination,
                    "fixed_global_order": incumbent["metadata"]["ordering"],
                    "global_order_sha256": object_digest(order), "max_chain_ops": 3}
            try:
                candidate = runs_plan(ir, order, changed)
            except ValueError as error:
                generation_log.append({"candidate": name, "status": "structure_rejected", "error": str(error), **meta})
                continue
            signature = object_digest(candidate)
            if signature in seen:
                generation_log.append({"candidate": name, "status": "duplicate_plan", **meta})
                continue
            seen.add(signature)
            routes = cross_routes(ir, changed, views)
            meta.update({"plan_object_sha256": signature,
                         "structural_cross_traffic_bytes": sum(routes.values()),
                         "structural_cross_traffic_delta": sum(routes.values()) - baseline_traffic,
                         "seed_route_removed": (row["tensor_id"], row["source_core"], row["target_core"]) not in routes,
                         "structural_estimate_excludes": "original-input replication, final writes, spill, timing and contention"})
            candidates.append({"name": name, "plan": candidate, "metadata": meta})
            generation_log.append({"candidate": name, "status": "generated", **meta})
            if len(candidates) == 8:
                break
        if len(candidates) == 8:
            break
    incumbent = {**incumbent, "plan": plan}
    provenance = {"prior_evaluations_path": str(source_path), "prior_evaluations_sha256": digest(source_path),
                  "selected_plan_path": str(selected_path), "selected_plan_file_sha256": digest(selected_path),
                  "incumbent_plan_file_sha256": digest(record["plan_path"]),
                  "incumbent_plan_object_sha256": object_digest(plan),
                  "official_result_sha256": digest(record["result_path"]),
                  "graph_sha256": digest(ir.path), "fixed_global_order_sha256": object_digest(order),
                  "all_transfer_count": len(ranked), "mapped_transfer_count": len(ranked),
                  "attribution": "saved tensor_id exists in original graph, unique direct compute producer; exact core-route and COPY timeline join"}
    return incumbent, candidates, ranked, generation_log, provenance


def metric_key(entry):
    metrics = entry["record"]["metrics"]
    return metrics["makespan"], metrics["data_movement_bytes"]["added_copy_bytes"]


def report(out, cases, attempts, budget):
    atomic_json(out / "summary.json", {"scope": "single-round two-case P2/N5 exploration", "cases": cases,
                                        "attempts": attempts, "evaluation_budget": budget})
    fields = ["case", "candidate", "status", "makespan", "added_copy_bytes", "cross_task_traffic",
              "elapsed_seconds", "record_path", "error"]
    with (out / "attempts.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(attempts)
    lines = ["# 末端跨核搬运：单轮有界邻域探针", "",
             "仅049和071、P2、5核，每图最多8个去重邻域；30秒/次、评估阶段总预算90秒、单worker。生成另计。",
             "COPY_IN结束越晚、释放后队列等待越长、字节越大则优先；这是候选排序特征，并非严格关键路径识别。",
             "从保存的HEFT最好方案独立出发，固定其全局计算拓扑序；仅移动单操作或至多3个操作的无分叉依赖链，按全局连续同核runs重建子图。没有二轮爬山。",
             "原tensor id、唯一compute producer、目标核实际消费者、COPY时间线与完整路由集合均交叉核对；无法唯一对应时直接拒绝，未猜测映射。", "",
             "|图|原makespan|本轮最佳makespan|保留incumbent后|原额外搬运B|选中额外搬运B|不增加时间且减少搬运的候选数|", "|---|---:|---:|---:|---:|---:|---:|"]
    for case in cases:
        lines.append("|{}|{}|{}|{}|{}|{}|{}|".format(case["case"], case["incumbent_makespan"],
                    case["best_new_makespan"], case["selected_makespan"], case["incumbent_added_copy_bytes"],
                    case["selected_added_copy_bytes"], len(case["non_slower_lower_copy_candidates"])))
    lines += ["", "选中顺序：官方成功后先最小makespan，再最小额外搬运；没有改善则保留旧incumbent。失败、负结果、未评估及去重记录全部保存。",
              "移动一条边的端点可能引入其他跨核边，改变输入复制、FIFO、内存复用与争用；减少结构跨核字节不能保证实际额外搬运或makespan下降。",
              "源incumbent已核图、方案、配置、官方源码及压缩原始结果哈希，直接引用已有官方成功记录；本轮未重复评估它，不把已有计算视作免费同预算优势。",
              "这是两个已看过图上的机制探针；不能推出100图收益、严格关键路径定位、同预算算法优势或国奖竞争力。",
              "", "预算与状态：`{}`".format(json.dumps(budget, ensure_ascii=False))]
    (out / "简报.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=HERE / "runs" / "critical_transfer_v1")
    parser.add_argument("--evaluation-budget", type=float, default=90)
    parser.add_argument("--generate-only", action="store_true")
    args = parser.parse_args(argv)
    if not 0 < args.evaluation_budget <= 90:
        parser.error("evaluation budget must be in (0,90]")
    out = args.run_dir.resolve()
    if DATA.parent.resolve() == out or DATA.parent.resolve() in out.parents:
        parser.error("run directory must be outside original attachments")
    if out.exists() and any(out.iterdir()):
        parser.error("run directory must be new or empty; do not overwrite prior evidence")
    out.mkdir(parents=True, exist_ok=True)
    sources = [Path(__file__).resolve(), HERE / "operation_heft_probe.py", HERE / "partition_candidates.py"]
    sources += [p for base in (SOLVER, OFFICIAL) for p in sorted(base.glob("*.py"))]
    source_hashes = {str(p): digest(p) for p in sources}
    generation_started = time.monotonic()
    datasets = []
    for case in CASES:
        ir = GraphIR.from_path(DATA / (case + ".json"))
        incumbent, candidates, ranked, generation_log, provenance = prepare(ir)
        directory = out / case
        atomic_json(directory / "incumbent.json", incumbent)
        atomic_json(directory / "incumbent.plan.json", incumbent["plan"])
        atomic_json(directory / "provenance.json", provenance)
        atomic_json(directory / "ranked_transfers.json", ranked)
        atomic_json(directory / "generation.json", generation_log)
        for candidate in candidates:
            atomic_json(directory / "plans" / (candidate["name"] + ".json"), candidate["plan"])
        datasets.append((ir, incumbent, candidates))
    manifest = {"source_sha256": source_hashes, "config_sha256": digest(DATA / "config.txt"),
                "cases": list(CASES), "problem": 2, "cores": 5, "single_call_timeout_cap": 30,
                "max_candidates_per_case": 8, "worker_concurrency": 1,
                "evaluation_budget_seconds": args.evaluation_budget,
                "generation_seconds": time.monotonic() - generation_started,
                "selection_rule": "successful official makespan first, added_copy_bytes second; keep incumbent",
                "priority_is_not_strict_critical_path": True}
    atomic_json(out / "manifest.json", manifest)
    if args.generate_only:
        print(json.dumps({"generated": {ir.path.stem: len(c) for ir, _, c in datasets}}))
        return 0
    started = time.monotonic()
    deadline = started + args.evaluation_budget
    attempts, cases, exhausted = [], [], False
    for ir, incumbent, candidates in datasets:
        evaluations, selected, best_new, joint = [], incumbent, None, []
        for candidate in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                exhausted = True
                record = {"status": "not_evaluated_budget", "metrics": {}, "elapsed_seconds": 0.0,
                          "record_path": None, "error": "evaluation-phase budget exhausted"}
            else:
                record = evaluate(ir.path, candidate["plan"], 2, out / "evaluations",
                                  timeout=min(30, remaining), config_path=DATA / "config.txt")
            row = {"candidate": candidate["name"], "metadata": candidate["metadata"], "record": record}
            evaluations.append(row)
            if record["status"] == "success":
                enriched = {**row, "plan": candidate["plan"]}
                if best_new is None or metric_key(enriched) < metric_key(best_new):
                    best_new = enriched
                if metric_key(enriched) < metric_key(selected):
                    selected = enriched
                if metric_key(enriched)[0] <= metric_key(incumbent)[0] and metric_key(enriched)[1] < metric_key(incumbent)[1]:
                    joint.append(candidate["name"])
            metrics = record.get("metrics", {})
            attempts.append({"case": ir.path.stem, "candidate": candidate["name"], "status": record["status"],
                             "makespan": metrics.get("makespan"),
                             "added_copy_bytes": metrics.get("data_movement_bytes", {}).get("added_copy_bytes"),
                             "cross_task_traffic": metrics.get("cross_task_traffic"),
                             "elapsed_seconds": record["elapsed_seconds"],
                             "record_path": record.get("record_path"), "error": record.get("error")})
            atomic_json(out / ir.path.stem / "all_evaluations.json", evaluations)
            print(json.dumps({"case": ir.path.stem, "candidate": candidate["name"], "status": record["status"],
                              "makespan": metrics.get("makespan"), "added_copy_bytes": attempts[-1]["added_copy_bytes"]}), flush=True)
        old_ms, old_bytes = metric_key(incumbent)
        new_ms, new_bytes = metric_key(selected)
        cases.append({"case": ir.path.stem, "incumbent_makespan": old_ms, "incumbent_added_copy_bytes": old_bytes,
                      "best_new_makespan": metric_key(best_new)[0] if best_new else None,
                      "selected_makespan": new_ms, "selected_added_copy_bytes": new_bytes,
                      "makespan_improved": new_ms < old_ms, "selected": selected,
                      "non_slower_lower_copy_candidates": joint,
                      "status_counts": dict(Counter(r["record"]["status"] for r in evaluations))})
        atomic_json(out / ir.path.stem / "selected.plan.json", selected["plan"])
        report(out, cases, attempts, {"limit_seconds": args.evaluation_budget,
                                     "elapsed_seconds": time.monotonic() - started, "exhausted": exhausted})
    unchanged = source_hashes == {str(p): digest(p) for p in sources}
    atomic_json(out / "completion.json", {"source_hashes_unchanged": unchanged,
                "elapsed_evaluation_phase_seconds": time.monotonic() - started, "completed": True,
                "budget_exhausted": exhausted, "statuses": dict(Counter(r["status"] for r in attempts)),
                "official_calls": sum(not r["record"].get("cache_hit", False)
                    for ir, _, _ in datasets for r in read_json(out / ir.path.stem / "all_evaluations.json")
                    if r["record"]["status"] != "not_evaluated_budget")})
    if not unchanged:
        raise RuntimeError("source changed during probe; results require review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
