"""Reproduce report thresholds and predeclared six-case timings from this snapshot."""
import csv
import json
from collections import Counter
from pathlib import Path
import statistics

HERE = Path(__file__).resolve().parent


def main():
    with (HERE / "trials.csv").open(encoding="utf-8-sig") as f:
        trials = list(csv.DictReader(f))
    with (HERE / "slots.csv").open(encoding="utf-8-sig") as f:
        slots = list(csv.DictReader(f))
    summary = json.loads((HERE / "summary.json").read_text(encoding="utf-8"))
    groups = []
    for problem in ("1", "2", "3"):
        rows = [r for r in trials if r["problem"] == problem and r["profile"] == "full" and r["cache_hit"] == "False"]
        for band in sorted({r["size_band"] for r in rows}):
            values = [float(r["wrapper_seconds"]) for r in rows if r["size_band"] == band]
            groups.append({"problem": int(problem), "size_band": band, "uncached_calls": len(values),
                           "median_wrapper_seconds": statistics.median(values), "mean_wrapper_seconds": statistics.mean(values)})
    threshold = []
    for problem in ("1", "2", "3"):
        rows = [r for r in trials if r["problem"] == problem and r["profile"] == "full" and r["cache_hit"] == "False"]
        threshold.append({"problem": int(problem),
                          "successful_calls_over_seconds": {str(t): sum(r["status"] == "success" and float(r["wrapper_seconds"]) > t for r in rows) for t in (30, 60, 90, 120)},
                          "timeout_cases": dict(Counter(r["case"] for r in rows if r["status"] == "timeout"))})
    cases = {"case_001", "case_011", "case_044", "case_049", "case_069", "case_071"}
    panel = [r for r in slots if r["case"] in cases and r["profile"] == "full" and r["problem"] == "2" and r["num_cores"] == "5"]
    result = {"source": "frozen slots.csv/trials.csv; no live reads or official calls", "uncached_call_cost_by_size": groups,
              "success_timeout_threshold_sensitivity": threshold,
              "earlier_predeclared_six_case_panel": {"cases": sorted(cases), "slots": len(panel),
                  "logical_calls": sum(int(r["logical_calls"]) for r in panel), "cache_hits": sum(int(r["cache_hits"]) for r in panel),
                  "summed_outer_child_seconds": sum(float(r["outer_child_elapsed_seconds"]) for r in panel),
                  "not_a_cold_new_candidate_prediction": True}}
    split = []
    for r in summary["problem_size_aggregates"]:
        if r["scope"] != "full_profile_only" or r["size_band"] != "all":
            continue
        official = r["official_function_seconds_completed_uncached"]
        worker = r["worker_seconds_completed_uncached"]
        split.append({"problem": r["problem"], "fresh_success_official_function_seconds": official,
                      "official_function_percent_of_outer_child": official / r["outer_child_elapsed_seconds"] * 100,
                      "fresh_success_worker_seconds": worker, "worker_minus_official_seconds": worker - official,
                      "timeout_wrapper_seconds_with_missing_official_function_time": r["timeout_wrapper_seconds"],
                      "cache_lookup_seconds_current": r["cache_lookup_seconds"],
                      "remaining_wrapper_seconds": r["wrapper_seconds"] - worker - r["timeout_wrapper_seconds"] - r["cache_lookup_seconds"]})
    result["official_worker_wrapper_split"] = split
    result["timeout_last_stages"] = dict(Counter(t["timeout_last_stage"] for t in summary["timeout_trials"]))
    (HERE / "supplementary_analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
