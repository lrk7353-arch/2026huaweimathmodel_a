"""Recover only the two authorized official single-core evaluations, in an isolated cache."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
sys.path.insert(0, str(ROOT / "A题研究/solver"))
from common import atomic_json, digest, read_json
from evaluator import evaluate


def run_case(case):
    print(json.dumps({"case": case, "status": "started", "problem": 0, "timeout": 1200}), flush=True)
    result = evaluate(ROOT / "选题分析/A题附件/data" / (case + ".json"), None, 0,
                      OUT / "evaluations", timeout=1200,
                      config_path=ROOT / "选题分析/A题附件/data/config.txt")
    atomic_json(OUT / case / "record.json", result)
    print(json.dumps({"case": case, "status": result["status"],
                      "makespan": result.get("metrics", {}).get("makespan"),
                      "elapsed_seconds": result["elapsed_seconds"], "cache_hit": result["cache_hit"],
                      "record_path": str(OUT / case / "record.json")}, ensure_ascii=False), flush=True)
    return result


def main():
    if (OUT / "manifest.json").exists():
        raise RuntimeError("Recovery directory already has a run manifest; do not silently overwrite it.")
    cases = ("case_072", "case_076")
    manifest = {"created_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version,
                "python_executable": sys.executable, "cases": list(cases), "workers": 2,
                "per_case_timeout_seconds": 1200, "problem": 0, "plan": None,
                "scope": "Original whole-graph official singlecore; no heuristic substitution",
                "original_failures": {}}
    for case in cases:
        source = ROOT / "A题研究/solver/runs/full_initial_v1/results" / case / "singlecore.json"
        old = read_json(source)
        atomic_json(OUT / "original_failures" / (case + ".json"), old)
        manifest["original_failures"][case] = {"source": str(source), "sha256": digest(source),
                                               "status": old["status"], "cache_key": old["cache_key"],
                                               "elapsed_seconds": old["elapsed_seconds"],
                                               "error": old.get("error")}
    atomic_json(OUT / "manifest.json", manifest)
    started = time.monotonic()
    records = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {pool.submit(run_case, case): case for case in cases}
        for job in as_completed(jobs):
            case = jobs[job]
            try:
                records[case] = job.result()
            except Exception as exc:
                records[case] = {"status": "runner_exception", "error": repr(exc)}
            atomic_json(OUT / "summary.json", {"records": records, "expected_cases": list(cases),
                                              "elapsed_seconds": time.monotonic() - started})
    lines = ["# 大图官方单核恢复评估", "", "仅复算072、076两张图；使用原版官方整图单核入口，plan=None、problem=0、每图1200秒上限、最多两并发。",
             "原300秒timeout记录保存在original_failures中，未删除或改写。输出完全独立于正在运行的full_initial_v1。", "",
             "| 图 | 状态 | 官方Makespan | 本次耗时秒 | 缓存复用 | 与原失败键相同 |", "|---|---|---:|---:|---|---|"]
    for case in cases:
        r = records[case]
        lines.append(f"| {case} | {r['status']} | {r.get('metrics', {}).get('makespan', '—')} | {r.get('elapsed_seconds', 0):.3f} | {r.get('cache_hit', False)} | {r.get('cache_key') == manifest['original_failures'][case]['cache_key']} |")
    lines += ["", "结果为success时才能由主任务按精确cache_key导入并续跑；timeout/runtime_error必须保留为未完成，不得替代成其他单核方案。", "",
              "[完整汇总](summary.json) · [运行清单及原失败引用](manifest.json)", ""]
    (OUT / "README.md").write_text("\n".join(lines), encoding="utf-8")
    return 0 if all(r["status"] == "success" for r in records.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
