#!/usr/bin/env python3
"""Bounded structural audit only: no official evaluation and no plan archive."""
import ast
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import signal
import sys
import time

sys.dont_write_bytecode = True
OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
ADVANCED = ROOT / "A题研究/探索"
SOLVER = ROOT / "A题研究/solver"
OFFICIAL = ROOT / "选题分析/A题附件/code"
DATA = OFFICIAL.parent / "data"
sys.path[:0] = [str(ADVANCED), str(SOLVER)]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def functions_digest(path, selected=None):
    module = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [n for n in module.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
             and (selected is None or n.name in selected)]
    return sha("\n".join(ast.dump(n, include_attributes=False) for n in nodes).encode())


GEN_SOURCES = {
    ADVANCED / "advanced_solve.py": {"generate_advanced_candidates", "_checked_incumbent"},
    ADVANCED / "operation_heft_probe.py": {"dependency_views", "operation_assignment", "runs_plan"},
    ADVANCED / "partition_candidates.py": {"topological_order", "contiguous_blocks", "block_views", "assign_blocks"},
    SOLVER / "baselines.py": None, SOLVER / "graph_ir.py": None,
    SOLVER / "plan.py": None, SOLVER / "common.py": None,
}


def source_snapshot():
    files = sorted(set(GEN_SOURCES) | set(SOLVER.glob("*.py")) | set(OFFICIAL.glob("*.py")))
    return {"full_files": {str(p): sha(p.read_bytes()) for p in files},
            "generation_ast": {str(p): functions_digest(p, selection) for p, selection in GEN_SOURCES.items()}}


def case_constant_scan():
    findings = []
    for path, selected in GEN_SOURCES.items():
        module = ast.parse(path.read_text(encoding="utf-8"))
        for node in module.body:
            if not isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                continue
            if selected is not None and node.name not in selected:
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Constant) and isinstance(child.value, str) and re.search(r"case_\d{3}", child.value):
                    findings.append({"path": str(path), "function": node.name, "line": child.lineno, "value": child.value})
    return findings


class AuditBudgetExpired(BaseException):
    pass


def expired(signum, frame):
    raise AuditBudgetExpired("295-second audit computation deadline")


def main():
    started = time.monotonic()
    before = source_snapshot()
    from advanced_solve import generate_advanced_candidates
    import advanced_solve
    import evaluator
    from graph_ir import GraphIR
    from common import atomic_json, object_digest
    from plan import validate_plan
    forbidden_calls = []

    def forbid_evaluate(*args, **kwargs):
        forbidden_calls.append("evaluate attempted")
        raise RuntimeError("official evaluation is forbidden in structural audit")

    advanced_solve.evaluate = forbid_evaluate
    evaluator.evaluate = forbid_evaluate
    graphs = sorted(DATA.glob("case_[0-9][0-9][0-9].json"))
    assert len(graphs) == 100, len(graphs)
    # Put the one requested repeatability check first; graph-ID is audit ordering,
    # not an input to or a special case inside candidate generation.
    graphs = sorted(graphs, key=lambda p: (p.stem != "case_071", p.name))
    rows, input_hashes = [], {}
    determinism = None
    interrupted = None
    current = None
    completed_graphs = 0
    case_constants = case_constant_scan()
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(0.01, 295 - (time.monotonic() - started)))
    try:
        for graph_path in graphs:
            current = {"case": graph_path.stem, "phase": "parse_graph"}
            parse_started = time.monotonic()
            ir = GraphIR.from_path(graph_path)
            parse_seconds = time.monotonic() - parse_started
            input_hashes[graph_path.name] = sha(graph_path.read_bytes())
            for num_cores in range(1, 6):
                current = {"case": graph_path.stem, "num_cores": num_cores, "phase": "generate_and_validate"}
                call_started = time.monotonic()
                try:
                    candidates, failures = generate_advanced_candidates(ir, num_cores)
                    signatures, active, families = [], [], Counter()
                    for candidate in candidates:
                        # A second lightweight validation explicitly attests the
                        # returned plans, independently of the inner add() calls.
                        validate_plan(ir, candidate["plan"])
                        assert len(candidate["plan"]["core_schedules"]) == num_cores
                        assert set(map(int, candidate["plan"]["node_to_subgraph"])) == set(ir.compute_ids)
                        signatures.append(object_digest(candidate["plan"]))
                        active.append(sum(bool(order) for order in candidate["plan"]["core_schedules"]))
                        families[candidate["metadata"]["family"]] += 1
                    assert len(signatures) == len(set(signatures))
                    assert candidates
                    rows.append({"case": graph_path.stem, "num_cores": num_cores,
                                 "status": "structurally_valid", "candidate_count": len(candidates),
                                 "active_cores_min": min(active), "active_cores_max": max(active),
                                 "family_counts": dict(families), "generation_failures": failures,
                                 "elapsed_seconds": time.monotonic() - call_started,
                                 "graph_parse_seconds": parse_seconds,
                                 "candidate_signature_sequence_sha256": sha(json.dumps(signatures).encode())})
                    if graph_path.stem == "case_071" and num_cores == 5:
                        current["phase"] = "determinism_repeat"
                        repeat_started = time.monotonic()
                        repeat, repeat_failures = generate_advanced_candidates(ir, num_cores)
                        match = (object_digest(candidates) == object_digest(repeat) and failures == repeat_failures)
                        determinism = {"case": "case_071", "num_cores": 5, "equal": match,
                                       "comparison": "entire candidate lists, metadata, plan order and failure lists",
                                       "repeat_elapsed_seconds": time.monotonic() - repeat_started}
                        assert match
                        del repeat
                    del candidates
                except Exception as error:
                    rows.append({"case": graph_path.stem, "num_cores": num_cores,
                                 "status": "audit_failed", "error": "{}: {}".format(type(error).__name__, error),
                                 "elapsed_seconds": time.monotonic() - call_started})
            completed_graphs += 1
            if completed_graphs % 10 == 0:
                atomic_json(OUT / "progress.json", {"complete_graphs": completed_graphs,
                                                     "completed_slots": len(rows),
                                                     "elapsed_seconds": time.monotonic() - started})
                print("completed_graphs={} slots={} elapsed={:.2f}s".format(
                    completed_graphs, len(rows), time.monotonic() - started), flush=True)
    except AuditBudgetExpired as error:
        interrupted = {**(current or {}), "reason": str(error)}
    except Exception as error:
        interrupted = {**(current or {}), "reason": "{}: {}".format(type(error).__name__, error)}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    after = source_snapshot()
    changed_files = [path for path in sorted(set(before["full_files"]) | set(after["full_files"]))
                     if before["full_files"].get(path) != after["full_files"].get(path)]
    changed_generation = [path for path in sorted(set(before["generation_ast"]) | set(after["generation_ast"]))
                          if before["generation_ast"].get(path) != after["generation_ast"].get(path)]
    valid = [row for row in rows if row["status"] == "structurally_valid"]
    seen_slots = {(r["case"], r["num_cores"]) for r in rows}
    missing = [{"case": p.stem, "num_cores": n} for p in graphs for n in range(1, 6)
               if (p.stem, n) not in seen_slots]
    summary = {
        "scope": "structural feasibility only; no official execution, timing score or generic optimality claim",
        "created_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version,
        "worker_count": 1, "requested_wallclock_cap_seconds": 300, "computation_timer_seconds": 295,
        "expected_graphs": 100, "expected_slots": 500, "completed_graphs": completed_graphs,
        "completed_slots": len(seen_slots), "valid_slots": len(valid),
        "complete": (len(valid) == 500 and not missing and not changed_generation and not forbidden_calls
                     and not any(row["status"] != "structurally_valid" for row in rows)
                     and bool(determinism and determinism["equal"])),
        "total_candidates_checked": sum(row["candidate_count"] for row in valid),
        "slots_with_generation_failures": sum(bool(row["generation_failures"]) for row in valid),
        "rows": rows, "missing_slots": missing, "interrupted": interrupted,
        "determinism_check": determinism, "official_evaluate_attempts": len(forbidden_calls),
        "case_specific_constants_in_generation_path": case_constants,
        "case_specific_scan_scope": "function/class AST nodes in generation dependency path; separate probe CLI main() is not executed",
        "input_graph_sha256": input_hashes, "source_start": before, "source_end": after,
        "changed_files": changed_files, "changed_generation_ast": changed_generation,
        "elapsed_seconds": time.monotonic() - started,
    }
    atomic_json(OUT / "summary.json", summary)
    lines = ["# Advanced候选生成结构验收", "", "仅验证正式图的结构覆盖与方案协议，不调用官方evaluate，不验证编译后的内存/Pipe/跨核执行可行性，不产生任何makespan成绩。", "",
             "- 正式100图×N=1..5，共500槽；本轮完成{}槽，结构有效{}槽，未覆盖{}槽。".format(len(seen_slots), len(valid), len(missing)),
             "- 核验候选共{}份；存在候选族生成失败的槽为{}。所有逐槽失败原样记录，不把未覆盖记成通过。".format(summary["total_candidates_checked"], summary["slots_with_generation_failures"]),
             "- Python {}，单进程，295秒计算硬截止；记录与收尾预留5秒。实际总耗时{:.3f}秒。".format(sys.version.split()[0], summary["elapsed_seconds"]),
             "- 官方evaluate调用尝试：{}。".format(len(forbidden_calls)),
             "- case_071、N=5候选重复生成一致性：{}。".format(determinism),
             "- 生成调用路径中按题号写死的字符串常量：{}。独立探针main()中的试验用例清单不在生成路径上。".format(case_constants),
             "- 开始/结束全文件哈希变化：{}。".format(changed_files),
             "- 生成相关函数AST变化：{}。".format(changed_generation), ""]
    if changed_files and not changed_generation:
        lines.append("完整文件有变化，但受检生成函数及依赖的AST不变；该情况与并行CLI路径修正相容，没有据此重跑大量生成。完整哈希均留档。")
    if interrupted:
        lines.append("墙钟截止时停止于：{}。剩余槽列在summary.json的missing_slots。".format(interrupted))
    lines.append("本验收不替代原版评估器。结构无环后仍可能因官方补入内存或逐Pipe顺序形成执行问题；耗时不是同预算算法性能实验。")
    (OUT / "结构验收.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("complete", "completed_slots", "valid_slots", "total_candidates_checked",
                                             "slots_with_generation_failures", "elapsed_seconds", "changed_files",
                                             "changed_generation_ast")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
