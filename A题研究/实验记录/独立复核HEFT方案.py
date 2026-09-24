"""Eight sequential evaluations: recheck selected P2 plans, then measure P3 on the same plans."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import json
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "A题研究/solver"))
from common import atomic_json, read_json
from evaluator import evaluate


def main():
    source = ROOT / "A题研究/探索/runs/operation_heft_v1"
    destination = ROOT / "A题研究/实验记录/independent_heft_check"
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError("Independent cache is not empty; do not overwrite the verification run.")
    destination.mkdir(parents=True, exist_ok=True)
    reported = {r["case"]: r for r in read_json(source / "summary.json")["cases"]}
    report = {"created_utc": datetime.now(timezone.utc).isoformat(), "workers": 1,
              "timeout_seconds": 60, "initial_cache_empty": True,
              "source_summary_sha256": hashlib.sha256((source / "summary.json").read_bytes()).hexdigest(),
              "scope": "P2 replay of four selected plans and paired P3 measurement on exactly those plans; no search",
              "cases": []}
    expected = {"case_051": (601168, 183148), "case_071": (14656, 10051),
                "case_064": (16384, 8945), "case_049": (142424, 113685)}
    for case, (old, new) in expected.items():
        selected_file = source / case / "selected.plan.json"
        plan = read_json(selected_file)
        singlecore = read_json(ROOT / "A题研究/solver/runs/pilot_fullpool_v2/results" / case / "singlecore.json")
        row = {"case": case, "plan_source": str(selected_file),
               "plan_file_sha256": hashlib.sha256(selected_file.read_bytes()).hexdigest(),
               "official_whole_graph_singlecore": singlecore["metrics"]["makespan"],
               "wcc_baseline": reported[case]["pilot_makespan"],
               "partition_baseline": reported[case]["partition_makespan"],
               "reported_selected_p2": reported[case]["selected_makespan"],
               "expected_partition": old, "expected_selected_p2": new,
               "configured_cores": len(plan["core_schedules"]),
               "active_cores_from_plan": sum(bool(s) for s in plan["core_schedules"]),
               "subgraphs": len(set(plan["node_to_subgraph"].values())), "evaluations": {}}
        report["cases"].append(row)
        for problem in (2, 3):
            record = evaluate(ROOT / "选题分析/A题附件/data" / (case + ".json"),
                              plan, problem, destination / "evaluations", timeout=60,
                              config_path=ROOT / "选题分析/A题附件/data/config.txt")
            row["evaluations"][str(problem)] = record
            atomic_json(destination / "verification.json", report)
            metric = record.get("metrics", {})
            print(json.dumps({"case": case, "problem": problem, "status": record["status"],
                              "cache_hit": record["cache_hit"], "makespan": metric.get("makespan"),
                              "active_cores": metric.get("active_cores"), "subgraphs": row["subgraphs"],
                              "movement": metric.get("data_movement_bytes"),
                              "cache_stats": metric.get("cache_stats")}, ensure_ascii=False), flush=True)
        p2, p3 = row["evaluations"]["2"], row["evaluations"]["3"]
        row["p2_matches_report"] = (p2["status"] == "success" and not p2["cache_hit"]
                                     and p2["metrics"]["makespan"] == new == row["reported_selected_p2"]
                                     and row["partition_baseline"] == old)
        if p2["status"] == p3["status"] == "success":
            row["p3_same_plan_hardware_ratio"] = p2["metrics"]["makespan"] / p3["metrics"]["makespan"]
            row["p2_reduction_from_partition"] = 1 - p2["metrics"]["makespan"] / old
            row["p2_reduction_from_wcc"] = 1 - p2["metrics"]["makespan"] / row["wcc_baseline"]
    report["all_p2_match"] = all(c["p2_matches_report"] for c in report["cases"])
    report["all_eight_fresh_success"] = all(r["status"] == "success" and not r["cache_hit"]
                                               for c in report["cases"] for r in c["evaluations"].values())
    atomic_json(destination / "verification.json", report)
    return 0 if report["all_p2_match"] and report["all_eight_fresh_success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
