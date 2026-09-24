"""Re-evaluate eight selected plans sequentially with an independent empty cache."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "A题研究/solver"))
from common import atomic_json, read_json
from evaluator import evaluate


def main():
    source = ROOT / "A题研究/探索/runs/partition_v1"
    destination = ROOT / "A题研究/实验记录/independent_partition_check"
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError("Independent cache is not empty; do not overwrite the verification run.")
    destination.mkdir(parents=True, exist_ok=True)
    reported = {(r["case"], r["problem"]): r for r in read_json(source / "summary.json")["problems"]}
    report = {"created_utc": datetime.now(timezone.utc).isoformat(), "workers": 1,
              "timeout_seconds": 60, "initial_cache_empty": True,
              "source_summary_sha256": hashlib.sha256((source / "summary.json").read_bytes()).hexdigest(),
              "checks": []}
    expected = {"case_071": (18919, 14656), "case_064": (19116, 16384),
                "case_051": (607628, 601168), "case_049": (160206, 142424)}
    for case in expected:
        for problem in (2, 3):
            selected_file = source / case / f"p{problem}_selected.plan.json"
            plan = read_json(selected_file)
            record = evaluate(ROOT / "选题分析/A题附件/data" / (case + ".json"),
                              plan, problem, destination / "evaluations", timeout=60,
                              config_path=ROOT / "选题分析/A题附件/data/config.txt")
            row = {"case": case, "problem": problem, "plan_source": str(selected_file),
                   "reported_baseline": reported[(case, problem)]["baseline_makespan"],
                   "reported_selected": reported[(case, problem)]["selected_makespan"],
                   "requested_expected": expected[case],
                   "configured_cores": len(plan["core_schedules"]),
                   "active_cores_from_plan": sum(bool(s) for s in plan["core_schedules"]),
                   "subgraphs": len(set(plan["node_to_subgraph"].values())),
                   "record": record}
            metrics = record.get("metrics", {})
            row["matches_report"] = (record["status"] == "success" and not record["cache_hit"]
                                      and metrics.get("makespan") == row["reported_selected"]
                                      and (row["reported_baseline"], row["reported_selected"]) == expected[case])
            report["checks"].append(row)
            atomic_json(destination / "verification.json", report)
            print(json.dumps({"case": case, "p": problem, "status": record["status"],
                              "cache_hit": record["cache_hit"], "match": row["matches_report"],
                              "makespan": metrics.get("makespan"),
                              "active_cores": metrics.get("active_cores"), "subgraphs": row["subgraphs"],
                              "movement": metrics.get("data_movement_bytes"),
                              "cross_task_traffic": metrics.get("cross_task_traffic"),
                              "cache_stats": metrics.get("cache_stats")}, ensure_ascii=False), flush=True)
    report["all_match"] = len(report["checks"]) == 8 and all(r["matches_report"] for r in report["checks"])
    atomic_json(destination / "verification.json", report)
    return 0 if report["all_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
