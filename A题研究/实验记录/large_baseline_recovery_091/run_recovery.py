"""One authorized original single-core replay for case_091, with an isolated cache."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
sys.path.insert(0, str(ROOT / "A题研究/solver"))
from common import atomic_json, digest, read_json
from evaluator import evaluate


def main():
    if (OUT / "manifest.json").exists():
        raise RuntimeError("Recovery run already exists; do not overwrite it.")
    case = "case_091"
    source = ROOT / "A题研究/solver/runs/full_initial_v1/results" / case / "singlecore.json"
    old = read_json(source)
    atomic_json(OUT / "original_failure.json", old)
    manifest = {"created_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version,
                "python_executable": sys.executable, "case": case, "workers": 1,
                "timeout_seconds": 1200, "problem": 0, "plan": None,
                "scope": "Original whole-graph official singlecore; no heuristic substitution",
                "original_failure": {"source": str(source), "sha256": digest(source),
                                     "status": old["status"], "cache_key": old["cache_key"],
                                     "elapsed_seconds": old["elapsed_seconds"], "error": old.get("error")}}
    atomic_json(OUT / "manifest.json", manifest)
    print(json.dumps({"case": case, "status": "started", "problem": 0, "timeout": 1200}), flush=True)
    result = evaluate(ROOT / "选题分析/A题附件/data" / (case + ".json"), None, 0,
                      OUT / "evaluations", timeout=1200,
                      config_path=ROOT / "选题分析/A题附件/data/config.txt")
    atomic_json(OUT / "record.json", result)
    summary = {"case": case, "status": result["status"], "makespan": result.get("metrics", {}).get("makespan"),
               "elapsed_seconds": result["elapsed_seconds"], "cache_hit": result["cache_hit"],
               "same_cache_key_as_failure": result.get("cache_key") == old["cache_key"],
               "record_path": str(OUT / "record.json"), "record": result}
    if result["status"] == "success":
        summary["compressed_result_sha_matches"] = digest(result["result_path"]) == result["result_sha256"]
    atomic_json(OUT / "summary.json", summary)
    lines = ["# case_091官方整图单核恢复评估", "",
             "使用原版wrapper和官方代码，plan=None、problem=0、timeout1200秒、单worker，独立缓存目录。原300秒timeout记录保留为original_failure.json，未改写原run。", "",
             "| 图 | 状态 | Makespan | 耗时秒 | 缓存复用 | 与原失败键相同 |", "|---|---|---:|---:|---|---|",
             f"| {case} | {result['status']} | {summary['makespan']} | {result['elapsed_seconds']:.3f} | {result['cache_hit']} | {summary['same_cache_key_as_failure']} |", "",
             "[真实评估记录](record.json) · [完整汇总](summary.json) · [保留的原失败](original_failure.json)", "",
             "只在status=success、缓存键与结果完整性均验证后导入原运行缓存；失败不能替换为其他单核方案。", ""]
    (OUT / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({k:v for k,v in summary.items() if k != "record"}, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
