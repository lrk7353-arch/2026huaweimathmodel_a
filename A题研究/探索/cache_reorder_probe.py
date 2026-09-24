#!/usr/bin/env python3
"""Bounded P3 probe: two graphs, fixed op-to-core allocation, <=8 reorder rules.

Only writes solver/runs/cache_reorder_v1. It does not change solver or official
sources, hardware configuration, input graphs, or the source pilot records.
"""
from collections import defaultdict, deque
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
SOLVER = ROOT / "A题研究/solver"
sys.path.insert(0, str(SOLVER))
from common import atomic_json, digest, object_digest, read_json
from evaluator import evaluate
from graph_ir import GraphIR
from plan import validate_plan

CASES = ("case_011", "case_093")
RULES = ("cyclic_core_offset", "cyclic_three_core_offset", "cyclic_even_spacing",
         "reverse_odd_cores", "light_first", "shared_group_block_rotation",
         "shared_group_round_robin", "light_first_with_phase")
OUT = SOLVER / "runs/cache_reorder_v1"
SOURCE = SOLVER / "runs/pilot_fullpool_v2/results"
DATA = ROOT / "选题分析/A题附件/data"
CONFIG = DATA / "config.txt"


def rotate(order, offset):
    if not order:
        return []
    offset %= len(order)
    return list(order[offset:]) + list(order[:offset])


def core_map(plan):
    by_sg = {sg: core for core, order in enumerate(plan["core_schedules"]) for sg in order}
    return {int(op): by_sg[sg] for op, sg in plan["node_to_subgraph"].items()}


def refine(ir, coarse):
    original = core_map(coarse)
    mapping, orders = {}, [[] for _ in coarse["core_schedules"]]
    assignment = {}
    for component in ir.components:
        cores = {original[op] for op in component.nodes}
        if len(cores) != 1:
            raise ValueError("source plan splits a WCC; preserving whole WCC and fixed allocation is impossible")
        core = next(iter(cores))
        assignment[component.id] = core
        orders[core].append(component.id)
        for op in component.nodes:
            mapping[str(op)] = component.id
    by_id = {c.id: c for c in ir.components}
    orders = [sorted(order, key=lambda cid: min(by_id[cid].nodes)) for order in orders]
    control = {"node_to_subgraph": {str(op): mapping[str(op)] for op in sorted(ir.compute_ids)},
               "core_schedules": orders}
    validate_plan(ir, control)
    assert core_map(control) == original
    return control, assignment


def generate_candidates(ir, control, assignment):
    by_id = {c.id: c for c in ir.components}
    input_cores = {tid: {assignment[cid] for cid in cids}
                   for tid, cids in ir.input_components.items()}
    shared = {tid for tid, cores in input_cores.items() if len(cores) > 1}
    signatures = {cid: tuple(sorted(by_id[cid].input_ids & shared)) for cid in by_id}
    active = [core for core, order in enumerate(control["core_schedules"]) if order]
    rank = {core: index for index, core in enumerate(active)}
    candidates = []
    for rule in RULES:
        schedules = []
        for core, original in enumerate(control["core_schedules"]):
            if not original:
                schedules.append([])
                continue
            r = rank[core]
            light = sorted(original, key=lambda cid: (by_id[cid].compute_work,
                                                      by_id[cid].input_bytes, min(by_id[cid].nodes)))
            if rule == "cyclic_core_offset":
                order = rotate(original, r)
            elif rule == "cyclic_three_core_offset":
                order = rotate(original, 3 * r)
            elif rule == "cyclic_even_spacing":
                order = rotate(original, len(original) * r // len(active))
            elif rule == "reverse_odd_cores":
                order = list(reversed(original)) if r % 2 else list(original)
            elif rule == "light_first":
                order = light
            elif rule == "light_first_with_phase":
                order = rotate(light, r)
            else:
                groups = defaultdict(list)
                for cid in original:
                    groups[signatures[cid]].append(cid)
                group_keys = rotate(sorted(groups), len(groups) * r // len(active))
                if rule == "shared_group_block_rotation":
                    order = [cid for key in group_keys for cid in groups[key]]
                else:
                    queues = [deque(groups[key]) for key in group_keys]
                    order = []
                    while any(queues):
                        for queue in queues:
                            if queue:
                                order.append(queue.popleft())
            schedules.append(order)
        candidate = {"node_to_subgraph": control["node_to_subgraph"], "core_schedules": schedules}
        validate_plan(ir, candidate)
        assert candidate["node_to_subgraph"] == control["node_to_subgraph"]
        assert core_map(candidate) == core_map(control)
        candidates.append((rule, candidate))
    return candidates, {"shared_root_input_count": len(shared),
                        "wccs_per_core": [len(order) for order in control["core_schedules"]],
                        "distinct_shared_signatures_per_core": [len({signatures[c] for c in order})
                                                                for order in control["core_schedules"]],
                        "shared_root_input_sizes": {str(t): ir.input_sizes[t] for t in sorted(shared)}}


def evaluate_plan(case, name, plan):
    folder = OUT / "results" / case
    atomic_json(folder / (name + ".plan.json"), plan)
    records = {}
    for problem in (2, 3):
        record = evaluate(DATA / (case + ".json"), plan, problem, OUT / "evaluations", timeout=30,
                          config_path=CONFIG)
        records["p" + str(problem)] = record
    result = {"name": name, "plan": plan, "plan_hash": object_digest(plan), "records": records,
              "status": "success" if all(r["status"] == "success" for r in records.values()) else "failed"}
    atomic_json(folder / (name + ".json"), result)
    print(json.dumps({"case": case, "name": name, "status": result["status"],
                      "p2": records["p2"]["metrics"].get("makespan"),
                      "p3": records["p3"]["metrics"].get("makespan"),
                      "hit_rate": records["p3"]["metrics"].get("cache_stats", {}).get("hit_rate")},
                     ensure_ascii=False), flush=True)
    return result


def t(result, problem):
    return result["records"]["p" + str(problem)]["metrics"]["makespan"]


def slim(result):
    return {"name": result["name"], "plan_hash": result["plan_hash"], "status": result["status"],
            "p2": result["records"]["p2"].get("metrics"),
            "p3": result["records"]["p3"].get("metrics"),
            "p2_record_path": result["records"]["p2"].get("record_path"),
            "p3_record_path": result["records"]["p3"].get("record_path")}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"scope": "bounded exploratory probe; not a full algorithm or 100-case result",
                "created_utc": datetime.now(timezone.utc).isoformat(), "cases": list(CASES),
                "fixed_rules_before_evaluation": list(RULES), "maximum_reorder_candidates_per_case": 8,
                "timeout_seconds": 30, "parallel_workers": 1, "fixed_config_path": str(CONFIG),
                "fixed_config_sha256": digest(CONFIG), "script_sha256": digest(__file__),
                "solver_py_sha256": {p.name: digest(p) for p in sorted(SOLVER.glob("*.py"))}}
    atomic_json(OUT / "manifest.json", manifest)
    summaries = []
    for case in CASES:
        started = time.perf_counter()
        ir = GraphIR.from_path(DATA / (case + ".json"))
        source_best = read_json(SOURCE / case / "affinity_p2_n5_seed0.json")
        pair = read_json(SOURCE / case / "affinity_cache_pair_n5_seed0.json")
        coarse = source_best["best"]["plan"]
        source_records = {"p2": pair["cells"]["t2_pi2"], "p3": pair["cells"]["t3_pi2"]}
        assert all(r["status"] == "success" for r in source_records.values())
        assert all(object_digest(coarse) == r["hashes"]["plan_sha256"] for r in source_records.values())
        original = {"name": "original_coarse", "plan": coarse, "plan_hash": object_digest(coarse),
                    "records": source_records, "status": "success"}
        atomic_json(OUT / "results" / case / "original_coarse.json", original)
        atomic_json(OUT / "results" / case / "original_coarse.plan.json", coarse)
        control, assignment = refine(ir, coarse)
        # Construct every order before observing its evaluation score.
        candidates, features = generate_candidates(ir, control, assignment)
        atomic_json(OUT / "results" / case / "candidate_manifest.json",
                    {"features": features, "control_plan_hash": object_digest(control),
                     "candidates": [{"name": name, "plan_hash": object_digest(plan)} for name, plan in candidates]})
        refined = evaluate_plan(case, "refined_control", control)
        scored = [original]
        if refined["status"] == "success":
            scored.append(refined)
        seen = {object_digest(control): "refined_control"}
        reorder_results, duplicate_rules = [], []
        for name, plan in candidates:
            plan_hash = object_digest(plan)
            if plan_hash in seen:
                duplicate_rules.append({"name": name, "identical_to": seen[plan_hash], "plan_hash": plan_hash})
                atomic_json(OUT / "results" / case / (name + ".plan.json"), plan)
                atomic_json(OUT / "results" / case / (name + ".duplicate.json"), duplicate_rules[-1])
                continue
            seen[plan_hash] = name
            result = evaluate_plan(case, name, plan)
            reorder_results.append(result)
            if result["status"] == "success":
                scored.append(result)
        # Stable tie handling keeps original/coarse or control, never promoting
        # a different plan on hit-rate alone.
        best = min(scored, key=lambda result: t(result, 3))
        valid_reorders = [r for r in reorder_results if r["status"] == "success"]
        best_reorder = min(valid_reorders, key=lambda result: t(result, 3)) if valid_reorders else None
        selected = best if t(best, 3) < t(original, 3) else original
        atomic_json(OUT / "results" / case / "retained.plan.json", selected["plan"])
        summary = {"case": case, "features": features, "original": slim(original), "control": slim(refined),
                   "best_reorder": slim(best_reorder) if best_reorder else None, "selected": slim(selected),
                   "reorder_rules_proposed": len(candidates), "unique_reorders_evaluated": len(reorder_results),
                   "duplicate_rules": duplicate_rules, "failed_candidates": [slim(r) for r in reorder_results if r["status"] != "success"],
                   "improves_original": t(selected, 3) < t(original, 3),
                   "reorder_improves_control": (best_reorder is not None and refined["status"] == "success"
                                                and t(best_reorder, 3) < t(refined, 3)),
                   "elapsed_seconds": time.perf_counter() - started,
                   "four_cell_original_vs_selected": {
                       "t2_pi_original": t(original, 2), "t3_pi_original": t(original, 3),
                       "t2_pi_selected": t(selected, 2), "t3_pi_selected": t(selected, 3)},
                   "four_cell_control_vs_best_reorder": ({
                       "t2_pi_control": t(refined, 2), "t3_pi_control": t(refined, 3),
                       "t2_pi_reorder": t(best_reorder, 2), "t3_pi_reorder": t(best_reorder, 3)}
                       if best_reorder is not None and refined["status"] == "success" else None)}
        summaries.append(summary)
        atomic_json(OUT / "summary.json", {"manifest": manifest, "completed_cases": summaries})
    lines = ["# 有上限的P3同核重排探针", "", "仅case_011、case_093。固定原P2最佳op→core分配；WCC整体细化，随后只重排相同mapping的core_schedules。所有硬件参数不变。每图事先固定8条规则，重复计划不重复评估，不根据成绩追加候选。", "",
             "| case | 方案 | P2 makespan | P3 makespan | P3字节命中率 |", "|---|---|---:|---:|---:|"]
    for summary in summaries:
        for label, key in [("原粗粒度", "original"), ("细化控制", "control"), ("最佳重排", "best_reorder")]:
            row = summary[key]
            if row is None or row["status"] != "success":
                lines.append("| {} | {} | 失败或无结果 | — | — |".format(summary["case"], label)); continue
            lines.append("| {} | {}（{}） | {} | {} | {:.4%} |".format(
                summary["case"], label, row["name"], row["p2"]["makespan"], row["p3"]["makespan"],
                row["p3"].get("cache_stats", {}).get("hit_rate", 0)))
    lines.extend(["", "## 保留与解释", ""])
    for summary in summaries:
        selected = summary["selected"]
        lines.append("- {}：保留 {}；P3相对原粗方案{}。{}个唯一重排候选，{}条重复规则；重排相对细化控制{}。".format(
            summary["case"], selected["name"], "实际下降" if summary["improves_original"] else "没有下降，维持原方案",
            summary["unique_reorders_evaluated"], len(summary["duplicate_rules"]),
            "确有下降" if summary["reorder_improves_control"] else "没有下降"))
    lines.extend(["", "四格原始数值见summary.json：原粗方案在P2/P3上、保留方案在P2/P3上；另列细化控制与最佳重排四格，以分离细化效应与顺序效应。不能将整个改善归给Cache，也不能将命中率上升替代makespan下降。",
                  "", "全部候选方案、重复关系、成功/失败记录、官方gzip时间线均留在本目录。该探针不能证明全100图收益，也不包含分核优化、内部WCC切分或大规模超参搜索。完成本次固定候选后停止扩展。", ""])
    (OUT / "P3重排探针报告.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"completed": len(summaries), "report": str(OUT / "P3重排探针报告.md")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
