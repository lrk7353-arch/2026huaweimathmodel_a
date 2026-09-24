#!/usr/bin/env python3
"""Bounded contiguous-topology splitting exploration, isolated from solver/.

Two orderings x three block counts x two assignments = at most 12 plans.
This is an exploratory static proxy, not a replacement for official evaluation.
"""
import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
import csv
import heapq
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SOLVER = HERE.parent / "solver"
sys.path.insert(0, str(SOLVER))
from graph_ir import GraphIR
from plan import validate_plan
from evaluator import evaluate
from common import DATA, OFFICIAL, atomic_json, digest, object_digest


def topological_order(ir, strategy):
    indegree = {op: len(ir.predecessors[op]) for op in ir.compute_ids}
    ready = [op for op in ir.compute_ids if indegree[op] == 0]
    heapq.heapify(ready)
    id_order = []
    while ready:
        op = heapq.heappop(ready)
        id_order.append(op)
        for nxt in ir.successors[op]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(ready, nxt)
    if len(id_order) != len(ir.compute_ids):
        raise ValueError("compute graph cycle")
    if strategy == "stable_id":
        return id_order
    if strategy != "critical_path":
        raise ValueError("unknown ordering")
    tail = {}
    for op in reversed(id_order):
        tail[op] = max(1, ir.ops[op]["cycles"]) + max(
            (tail[nxt] for nxt in ir.successors[op]), default=0)
    indegree = {op: len(ir.predecessors[op]) for op in ir.compute_ids}
    ready = [(-tail[op], op) for op in ir.compute_ids if indegree[op] == 0]
    heapq.heapify(ready)
    result = []
    while ready:
        _, op = heapq.heappop(ready)
        result.append(op)
        for nxt in ir.successors[op]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(ready, (-tail[nxt], nxt))
    return result


def contiguous_blocks(ir, order, target):
    """Quantiles of cumulative normalized M/V work; exactly target nonempty blocks.

    mass(op)=cycles/total_M for M, cycles/total_V for V. Both nonempty
    pipelines contribute total mass 1, so neither is ignored when cutting.
    """
    target = min(target, len(order))
    if target < 1:
        raise ValueError("need at least one compute op")
    prefix = [0.0]
    for op in order:
        node = ir.ops[op]
        total = ir.total_work_m if node["pipe"] == "PIPE_M" else ir.total_work_v
        prefix.append(prefix[-1] + max(1, node["cycles"]) / max(total, 1))
    cuts = [0]
    for k in range(1, target):
        lower, upper = cuts[-1] + 1, len(order) - (target - k)
        objective = prefix[-1] * k / target
        near = bisect_left(prefix, objective, lower, upper + 1)
        options = {max(lower, min(upper, near)), max(lower, min(upper, near - 1))}
        cuts.append(min(options, key=lambda j: (abs(prefix[j] - objective), j)))
    cuts.append(len(order))
    return [order[a:b] for a, b in zip(cuts, cuts[1:])]


def block_views(ir, blocks):
    mapping = {op: bid for bid, block in enumerate(blocks) for op in block}
    preds = [set() for _ in blocks]
    for op in ir.compute_ids:
        for nxt in ir.successors[op]:
            a, b = mapping[op], mapping[nxt]
            if a != b:
                if a >= b:
                    raise ValueError("blocks must follow global topology")
                preds[b].add(a)
    producers, consumers = defaultdict(set), defaultdict(set)
    for edge in ir.graph["edges"]:
        a, b = edge["source"], edge["target"]
        if a in ir.ops:
            producers[b].add(a)
        else:
            consumers[a].add(b)
    all_succ = {op: set() for op in ir.ops}
    for tid in ir.tensors:
        for op in producers[tid]:
            all_succ[op].update(consumers[tid])
    root_inputs = [set() for _ in blocks]
    for tid in ir.input_sizes:
        stack, seen = list(consumers[tid]), set()
        while stack:
            op = stack.pop()
            if op in seen:
                continue
            seen.add(op)
            if op in mapping:
                root_inputs[mapping[op]].add(tid)
            else:
                stack.extend(all_succ[op])
    incoming = [dict() for _ in blocks]
    boundary_bytes = [0] * len(blocks)
    # Real data have no internal COPY chains between compute nodes (audited).
    # DAG precedence above remains exact; bytes below use original direct tensors.
    for tid, tensor in ir.tensors.items():
        sources = {mapping[p] for p in producers[tid] if p in mapping}
        targets = {mapping[c] for c in consumers[tid] if c in mapping}
        for target in targets:
            previous = sources - {target}
            if previous:
                incoming[target][tid] = (tensor["size"], tuple(sorted(previous)))
                boundary_bytes[target] += tensor["size"]
    work = []
    for block in blocks:
        wm = sum(max(1, ir.ops[i]["cycles"]) for i in block if ir.ops[i]["pipe"] == "PIPE_M")
        wv = sum(max(1, ir.ops[i]["cycles"]) for i in block if ir.ops[i]["pipe"] == "PIPE_V")
        local_ends = {}
        for op in block:
            local_ends[op] = max(1, ir.ops[op]["cycles"]) + max(
                (local_ends[p] for p in ir.predecessors[op] if p in local_ends), default=0)
        work.append((wm, wv, max(wm, wv, max(local_ends.values(), default=0))))
    return {"mapping": mapping, "preds": preds, "root_inputs": root_inputs,
            "incoming": incoming, "boundary_bytes": boundary_bytes, "work": work}


def assign_blocks(ir, blocks, view, strategy, num_cores=5):
    assignment, ends = [], []
    available = [0.0] * num_cores
    load_m, load_v = [0] * num_cores, [0] * num_cores
    core_inputs = [set() for _ in range(num_cores)]
    cross_pairs = set()
    total_bytes = 0
    for bid, _ in enumerate(blocks):
        wm, wv, duration = view["work"][bid]
        def prospective(core):
            fresh = sum(ir.input_sizes[t] for t in view["root_inputs"][bid] if t not in core_inputs[core])
            pairs = set()
            for tid, (size, source_blocks) in view["incoming"][bid].items():
                for source in {assignment[p] for p in source_blocks}:
                    if source != core and (tid, source, core) not in cross_pairs:
                        pairs.add((tid, source, core))
                        fresh += 2 * size
            return fresh, pairs
        choices = []
        for core in range(num_cores):
            fresh, pairs = prospective(core)
            if strategy == "communication_eft":
                # P1-like proxy only: tasks serialized per core; cross Task
                # release uses complete predecessor duration + 1000 cycles.
                release = max((ends[p] + (1000 if assignment[p] != core else 0)
                               for p in view["preds"][bid]), default=0)
                start = max(available[core] + (100 if available[core] else 0), release)
                reads = view["boundary_bytes"][bid] + sum(ir.input_sizes[t] for t in view["root_inputs"][bid])
                predicted_end = start + duration + reads / 60.0
                key = (predicted_end, fresh, load_m[core] + load_v[core], core)
            elif strategy == "balanced_affinity":
                pm, pv = list(load_m), list(load_v)
                pm[core] += wm
                pv[core] += wv
                peak = max(pm + pv)
                key = (max(peak, (total_bytes + fresh) / 60.0) + 0.25 * (total_bytes + fresh) / 60.0,
                       max(pm[core], pv[core]), fresh, core)
                predicted_end = available[core] + duration
            else:
                raise ValueError("unknown assignment")
            choices.append((key, core, predicted_end, fresh, pairs))
        _, core, end, fresh, pairs = min(choices, key=lambda item: item[0])
        assignment.append(core)
        ends.append(end)
        available[core] = end
        load_m[core] += wm
        load_v[core] += wv
        total_bytes += fresh
        cross_pairs.update(pairs)
        core_inputs[core].update(view["root_inputs"][bid])
    return assignment, {"proxy_core_work_m": load_m, "proxy_core_work_v": load_v,
                        "proxy_input_and_pair_copy_bytes": total_bytes,
                        "proxy_scope": "static bound/priority only; excludes spill, shared DDR timing, Pipe FIFO and cache"}


def generate_partition_candidates(ir, num_cores=5):
    if num_cores != 5:
        raise ValueError("this exploration is restricted to N=5")
    candidates, seen = [], {}
    for ordering in ("critical_path", "stable_id"):
        order = topological_order(ir, ordering)
        for target in (5, 10, 20):
            blocks = contiguous_blocks(ir, order, target)
            view = block_views(ir, blocks)
            for assignment_method in ("communication_eft", "balanced_affinity"):
                assignment, proxy = assign_blocks(ir, blocks, view, assignment_method, num_cores)
                schedules = [[] for _ in range(num_cores)]
                for bid, core in enumerate(assignment):
                    schedules[core].append(bid)
                plan = {"node_to_subgraph": {str(op): bid for bid, block in enumerate(blocks) for op in block},
                        "core_schedules": schedules}
                validate_plan(ir, plan)
                metadata = {"ordering": ordering, "target_blocks": target, "actual_blocks": len(blocks),
                            "assignment": assignment_method, "block_core_assignment": assignment,
                            "block_compute_counts": list(map(len, blocks)),
                            "active_cores": sum(bool(s) for s in schedules), **proxy}
                signature = object_digest(plan)
                if signature in seen:
                    seen[signature]["metadata"]["also_generated_as"].append(metadata)
                    continue
                metadata["also_generated_as"] = []
                record = {"name": "{}_b{}_{}".format(ordering, target, assignment_method),
                          "plan": plan, "metadata": metadata}
                seen[signature] = record
                candidates.append(record)
    assert len(candidates) <= 12
    return candidates


def metric_key(record):
    return (record["metrics"]["makespan"], record["metrics"].get("data_movement_bytes", {}).get("added_copy_bytes", 0))


def _write_summary(out, summaries, attempts):
    atomic_json(out / "summary.json", {"scope": "Four selected cases, N=5 exploration; not official 100-case benchmark",
                                      "problems": summaries, "attempts": attempts})
    with (out / "attempts.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        fields = ["case", "problem", "kind", "candidate", "status", "makespan", "added_copy_bytes", "elapsed_seconds", "error", "record_path"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(attempts)
    lines = ["# 连续拓扑块切分探索结果", "", "仅四个指定用例、5核，未并入正式solver，也不代表100图效果。所有候选和失败均保存。", "",
             "切分预算：每图至多12份计划，两种拓扑序×5/10/20块×两种分核；每份计划分别评估指定问题。",
             "对照为pilot已评估simple5核最好方案，独立重评确认；case049的原pilot受6次候选预算限制，并非穷尽simple候选。", "",
             "|用例|问题|对照|切分最好|保留对照后最好|时间降低|切分状态|", "|---|---:|---:|---:|---:|---:|---|"]
    for s in summaries:
        baseline = s["baseline_makespan"]
        improved = s["selected_makespan"]
        reduction = "{:.2%}".format(1 - improved / baseline) if baseline and improved else "NA"
        lines.append("|{}|{}|{}|{}|{}|{}|{}|".format(s["case"], s["problem"], baseline,
                     s["best_partition_makespan"], improved, reduction, s["partition_status_counts"]))
    lines += ["", "局限：连续拓扑区间可能割断并行分支、制造串行Task依赖；静态均衡代理不描述真实共享带宽、内存复用和FIFO等待。",
              "P1结构无环不保证P2/P3编译后的全局执行无环；官方拒绝的候选不计为成绩。最优方案始终允许保留原对照。"]
    (out / "摘要.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", nargs="+", default=["071", "064", "051", "049"])
    parser.add_argument("--problems", type=int, nargs="+", default=[1, 2, 3])
    parser.add_argument("--run-dir", type=Path, default=HERE / "runs" / "partition_v1")
    parser.add_argument("--pilot", type=Path, default=SOLVER / "runs" / "pilot_v1" / "results")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--generate-only", action="store_true")
    args = parser.parse_args(argv)
    out = args.run_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if any(p not in (1, 2, 3) for p in args.problems):
        parser.error("problems must be 1/2/3")
    started = time.perf_counter()
    summaries, attempts = [], []
    manifest = {"script_sha256": digest(Path(__file__)), "source_hashes": {
        str(p): digest(p) for base in (SOLVER, OFFICIAL) for p in sorted(base.glob("*.py"))},
        "config_sha256": digest(DATA / "config.txt"), "cases": args.cases, "problems": args.problems,
        "num_cores": 5, "max_partition_candidates_per_case": 12, "worker_concurrency": 1,
        "per_evaluation_timeout": args.timeout, "prior_case_selection": "user-specified difficult cases, not random sample"}
    atomic_json(out / "manifest.json", manifest)
    for raw_case in args.cases:
        case = raw_case if raw_case.startswith("case_") else "case_" + raw_case.zfill(3)
        ir = GraphIR.from_path(DATA / (case + ".json"))
        t0 = time.perf_counter()
        candidates = generate_partition_candidates(ir)
        atomic_json(out / case / "generation.json", {"seconds": time.perf_counter() - t0,
                    "graph_sha256": digest(ir.path), "candidate_count": len(candidates),
                    "candidates": [{"name": c["name"], "metadata": c["metadata"]} for c in candidates]})
        for candidate in candidates:
            atomic_json(out / case / "plans" / (candidate["name"] + ".json"), candidate["plan"])
        if args.generate_only:
            print(json.dumps({"case": case, "generated": len(candidates)}), flush=True)
            continue
        for problem in args.problems:
            pilot_path = args.pilot / case / "simple_p{}_n5_seed0.json".format(problem)
            pilot = json.loads(pilot_path.read_text(encoding="utf-8"))
            original_best = pilot.get("best")
            if not original_best:
                raise ValueError("pilot lacks successful baseline: " + str(pilot_path))
            baseline_plan = original_best["plan"]
            baseline_record = original_best["record"]
            old_hashes = baseline_record["hashes"]
            if old_hashes["graph_sha256"] != digest(ir.path) or old_hashes["config_sha256"] != digest(DATA / "config.txt"):
                raise ValueError("pilot input/config hash mismatch")
            if old_hashes["official_py_sha256"] != {p.name: digest(p) for p in sorted(OFFICIAL.glob("*.py"))}:
                raise ValueError("pilot official source mismatch")
            baseline = {"name": "pilot_simple_best", "plan": baseline_plan,
                        "metadata": {"pilot_path": str(pilot_path), "pilot_candidate_count": pilot["candidate_count"],
                                     "pilot_evaluated_count": pilot["evaluated_count"], "pilot_stop_reason": pilot["stop_reason"]}}
            pool = [baseline] + candidates
            evaluated, best, best_partition = [], None, None
            for candidate in pool:
                kind = "baseline" if candidate is baseline else "partition"
                record = evaluate(ir.path, candidate["plan"], problem, out / "evaluations", timeout=args.timeout)
                row = {"kind": kind, "candidate": candidate["name"], "metadata": candidate["metadata"], "record": record}
                evaluated.append(row)
                if record["status"] == "success":
                    if best is None or metric_key(record) < metric_key(best["record"]):
                        best = {**row, "plan": candidate["plan"]}
                    if kind == "partition" and (best_partition is None or metric_key(record) < metric_key(best_partition["record"])):
                        best_partition = {**row, "plan": candidate["plan"]}
                metrics = record.get("metrics", {})
                attempts.append({"case": case, "problem": problem, "kind": kind,
                                 "candidate": candidate["name"], "status": record["status"],
                                 "makespan": metrics.get("makespan"),
                                 "added_copy_bytes": metrics.get("data_movement_bytes", {}).get("added_copy_bytes"),
                                 "elapsed_seconds": record["elapsed_seconds"], "error": record.get("error"),
                                 "record_path": record["record_path"]})
                atomic_json(out / case / "p{}_all_evaluations.json".format(problem), evaluated)
                print(json.dumps({"case": case, "problem": problem, "candidate": candidate["name"],
                                  "status": record["status"], "makespan": metrics.get("makespan")}), flush=True)
            base = evaluated[0]["record"]
            if base["status"] == "success" and base["metrics"]["makespan"] != baseline_record["metrics"]["makespan"]:
                raise ValueError("independent baseline replay disagrees with pilot")
            summary = {"case": case, "problem": problem,
                       "baseline_makespan": base.get("metrics", {}).get("makespan"),
                       "best_partition_makespan": best_partition["record"]["metrics"]["makespan"] if best_partition else None,
                       "selected_makespan": best["record"]["metrics"]["makespan"] if best else None,
                       "selected_candidate": best["candidate"] if best else None,
                       "partition_status_counts": dict(Counter(r["record"]["status"] for r in evaluated if r["kind"] == "partition")),
                       "best": best, "best_partition": best_partition, "baseline_source": baseline["metadata"]}
            if best:
                atomic_json(out / case / "p{}_selected.plan.json".format(problem), best["plan"])
            summaries.append(summary)
            _write_summary(out, summaries, attempts)
    atomic_json(out / "completion.json", {"elapsed_seconds": time.perf_counter() - started,
                                           "evaluated_count": len(attempts), "completed": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
