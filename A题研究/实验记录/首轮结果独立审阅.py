"""Read-only inspection of completed benchmark cases; writes only this directory."""
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inspect_run(name):
    run = ROOT / "A题研究/solver/runs" / name
    manifest, progress = read(run / "manifest.json"), read(run / "progress.json")
    declared = manifest["specification"]
    completed = [item["case"] for item in progress["completed"]]
    rows, attempts, baselines, failed_attempts, cells = [], Counter(), {}, [], []
    per_core_candidates = {}
    mismatches = []
    for case in completed:
        directory = run / "results" / case
        baseline = read(directory / "singlecore.json")
        baselines[case] = baseline
        for method in declared["methods"]:
            for problem in declared["problems"]:
                for cores in declared["cores"]:
                    filename = directory / f"{method}_p{problem}_n{cores}_seed{declared['seed']}.json"
                    if not filename.exists():
                        mismatches.append({"case": case, "missing": str(filename)})
                        continue
                    result = read(filename)
                    if result["benchmark_signature"] != manifest["signature"]:
                        mismatches.append({"case": case, "signature": str(filename)})
                    for attempt in result["evaluations"]:
                        record = attempt["record"]
                        attempts[record["status"]] += 1
                        if record["status"] != "success":
                            failed_attempts.append({"case": case, "method": method, "problem": problem,
                                                    "cores": cores, "candidate": attempt["candidate"],
                                                    "status": record["status"], "error": record.get("error")})
                        if (record["status"] == "success" and problem in (1, 2)
                                and attempt["metadata"].get("granularity") == "per_core"):
                            key = (case, method, cores, attempt["plan_hash"])
                            per_core_candidates.setdefault(key, {})[problem] = record["metrics"]["makespan"]
                    best = result["best"]
                    if not best or baseline["status"] != "success":
                        continue
                    metric = best["record"]["metrics"]
                    original = baseline["metrics"]["makespan"]
                    rows.append({"case": case, "method": method, "problem": problem, "cores": cores,
                                 "baseline": original, "makespan": metric["makespan"],
                                 "ratio": original / metric["makespan"], "candidate": best["candidate"],
                                 "plan_hash": best["plan_hash"], "active_cores": metric["active_cores"],
                                 "traffic": metric["data_movement_bytes"],
                                 "baseline_traffic": baseline["metrics"]["data_movement_bytes"],
                                 "cross_task_traffic": metric.get("cross_task_traffic"),
                                 "cache_stats": metric.get("cache_stats"),
                                 "candidate_count": result["candidate_count"],
                                 "evaluated_count": result["evaluated_count"],
                                 "result_path": best["record"]["result_path"],
                                 "result_sha256": best["record"]["result_sha256"]})
            for cores in declared["cores"]:
                pairfile = directory / f"{method}_cache_pair_n{cores}_seed{declared['seed']}.json"
                if pairfile.exists():
                    pair = read(pairfile)
                    cells.append({"case": case, "method": method, "cores": cores,
                                  "ratios": pair["ratios"],
                                  "cell_statuses": {k: v["status"] for k, v in pair["cells"].items()}})
    bykey = {(r["case"], r["method"], r["problem"], r["cores"]): r for r in rows}
    scene_pairs, method_pairs = [], []
    for row in rows:
        if row["problem"] == 1:
            other = bykey.get((row["case"], row["method"], 2, row["cores"]))
            if other:
                scene_pairs.append({"case": row["case"], "method": row["method"], "cores": row["cores"],
                                    "p1": row["makespan"], "p2": other["makespan"],
                                    "p1_candidate": row["candidate"], "p2_candidate": other["candidate"]})
        if row["method"] == "simple":
            other = bykey.get((row["case"], "affinity", row["problem"], row["cores"]))
            if other:
                method_pairs.append({"case": row["case"], "problem": row["problem"], "cores": row["cores"],
                                     "simple": row["makespan"], "affinity": other["makespan"],
                                     "speed_ratio": row["makespan"] / other["makespan"],
                                     "time_reduction_fraction": 1 - other["makespan"] / row["makespan"]})
    comparable = [v for v in per_core_candidates.values() if set(v) == {1, 2}]
    # These read back compressed official result artifacts. They do not re-run evaluators.
    checked = []
    selected_cases = {"case_001", "case_006", "case_010", "case_012", "case_015", "case_019"}
    seen_paths = set()
    for row in rows:
        if ((row["case"] not in selected_cases and row["ratio"] <= row["cores"])
                or row["result_path"] in seen_paths):
            continue
        seen_paths.add(row["result_path"])
        with gzip.open(row["result_path"], "rt", encoding="utf-8") as stream:
            raw = json.load(stream)
        checked.append({"case": row["case"], "problem": row["problem"], "cores": row["cores"],
                        "sha_match": sha(row["result_path"]) == row["result_sha256"],
                        "makespan_match": raw["makespan"] == row["makespan"],
                        "movement_match": raw["data_movement_bytes"] == row["traffic"],
                        "scene_match": raw["scene"] == ("A" if row["problem"] == 1 else "B")})
    return {"run": name, "manifest_signature": manifest["signature"], "settings": declared,
            "completed_cases": completed, "expected_cases": len(declared["inputs"]),
            "complete_graph_count": sum(p["complete"] for p in progress["completed"]),
            "progress_failures": progress["failures"], "successful_slots": len(rows),
            "attempt_status_counts": dict(attempts), "failed_attempts": failed_attempts,
            "mismatches": mismatches, "rows": rows, "scene_pairs": scene_pairs,
            "method_pairs": method_pairs, "cache_pairs": cells,
            "supra_core_ratios": [r for r in rows if r["ratio"] > r["cores"] + 1e-12],
            "scene_equal_count": sum(p["p1"] == p["p2"] for p in scene_pairs),
            "per_core_candidate_comparisons": len(comparable),
            "per_core_candidate_equal_count": sum(p[1] == p[2] for p in comparable),
            "raw_artifact_checks": checked}


def main():
    old_inputs = read(ROOT / "A题研究/方案审阅/结构核验/a_structure_summary.json")["input_sha256"]
    old_official = read(ROOT / "A题研究/方案审阅/cache_microtests/provenance.json")["files_sha256"]
    snapshot = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Read-only snapshot of cases already marked completed in progress.json; unfinished cases excluded explicitly.",
        "input_hash_checks": {name: sha(ROOT / "选题分析/A题附件/data" / name) == expected
                              for name, expected in old_inputs.items()},
        "official_hash_checks": {name: sha(ROOT / name) == expected for name, expected in old_official.items()},
        "runs": [inspect_run(name) for name in ("full_initial_v1", "pilot_fullpool_v2")],
    }
    target = OUT / "首轮结果独立审阅_快照.json"
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    concise = {"created_utc": snapshot["created_utc"],
               "input_hash_matches": sum(snapshot["input_hash_checks"].values()),
               "official_hash_matches": sum(snapshot["official_hash_checks"].values()),
               "runs": [{"run": r["run"], "completed": len(r["completed_cases"]),
                         "expected": r["expected_cases"], "slots": r["successful_slots"],
                         "attempts": r["attempt_status_counts"], "mismatches": r["mismatches"],
                         "p1p2_equal": [r["scene_equal_count"], len(r["scene_pairs"])],
                         "same_per_core_equal": [r["per_core_candidate_equal_count"], r["per_core_candidate_comparisons"]],
                         "raw_checks": len(r["raw_artifact_checks"]),
                         "raw_check_failures": [c for c in r["raw_artifact_checks"] if not all(v for k,v in c.items() if k.endswith('_match'))],
                         "method_differences": [p for p in r["method_pairs"] if p["simple"] != p["affinity"]],
                         "p3_differences": [p for p in r["cache_pairs"] if any(v != 1 for v in p["ratios"].values())]}
                        for r in snapshot["runs"]]}
    print(json.dumps(concise, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
