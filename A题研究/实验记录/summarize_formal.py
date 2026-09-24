#!/usr/bin/env python3
"""Audit frozen batch results and emit complete-denominator numerical evidence.

Partial collections expose coverage only; no 100-case mean is emitted until all
100 successful, completed, verified slots in the corresponding group exist.
"""
import argparse
from collections import Counter, defaultdict
import csv
import gzip
import json
import math
from pathlib import Path
import statistics
import sys

RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
from solver.common import read_json, atomic_json, digest, object_digest, DATA, OFFICIAL
from advanced_solver.batch import verify_summary, _signature


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                             for k, v in row.items()})


def baselines():
    results = {}
    for i in range(1, 101):
        case = f"case_{i:03d}"
        path = RESEARCH / "solver/runs/full_initial_v1/results" / case / "singlecore.json"
        r = read_json(path)
        if r["status"] != "success" or r["problem"] != 0:
            raise ValueError(f"missing official baseline {case}")
        if digest(r["result_path"]) != r["result_sha256"] or digest(DATA / (case + ".json")) != r["hashes"]["graph_sha256"]:
            raise ValueError(f"baseline hash mismatch {case}")
        with gzip.open(r["result_path"], "rt", encoding="utf-8") as f:
            raw = json.load(f)
        if raw["makespan"] != r["metrics"]["makespan"]:
            raise ValueError("baseline metrics mismatch")
        results[case] = r["metrics"]["makespan"]
    return results


def summarize(root, out, batches=None):
    baseline = baselines()
    rows, trials, stages, sources, errors = [], [], [], [], []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not (directory / "manifest.json").exists():
            continue
        if batches and directory.name not in batches:
            continue
        manifest = read_json(directory / "manifest.json")
        if "settings" not in manifest:
            continue
        settings = manifest["settings"]
        report_path = directory / "summary.json"
        if not report_path.exists():
            report_path = directory / "progress.json"
        reports = read_json(report_path)["slots"] if report_path.exists() else []
        reported = {r["slot"]: r for r in reports}
        sources.append({"path": str(directory / "manifest.json"), "sha256": digest(directory / "manifest.json")})
        for c in settings["cases"]:
            case = f"case_{c:03d}"
            for p in settings["problems"]:
                for n in settings["cores"]:
                    slot = f"{case}_p{p}_n{n}"
                    row = {"batch": directory.name, "profile": settings["profile"], "seed": settings["seed"],
                           "case": case, "problem": p, "num_cores": n, "baseline_makespan": baseline[case],
                           "logical_cap": settings["max_evaluations"], "status": "pending",
                           "makespan": None, "speedup": None, "verified_complete": False}
                    report = reported.get(slot)
                    if report:
                        row["status"] = report["outcome"]
                    if not report or not report.get("attempt_dir"):
                        rows.append(row)
                        continue
                    attempt = Path(report["attempt_dir"])
                    try:
                        launch = read_json(attempt.with_name(attempt.name + ".launch.json"))
                        # Signature was recorded before execution. Warm-start
                        # sources are explicitly in this immutable launch file.
                        signature = launch["signature"]
                        if signature["graph_sha256"] != digest(DATA / (case + ".json")):
                            raise ValueError("input graph changed")
                        if signature["config_sha256"] != digest(signature["config_path"]):
                            raise ValueError("configuration changed")
                        result = verify_summary(attempt, signature)
                        for file, h in signature["source_sha256"].items():
                            if digest(file) != h:
                                raise ValueError("source changed since this frozen run: " + file)
                        row["verified_complete"] = bool(result["completed"] and result.get("best"))
                        row["summary_path"] = str(attempt / "summary.json")
                        row["summary_sha256"] = digest(attempt / "summary.json")
                        row["evaluations"] = result["evaluated_count"]
                        row["official_calls"] = result.get("official_calls")
                        row["cache_hits"] = result.get("cache_hits")
                        row["solver_elapsed_seconds"] = result.get("elapsed_seconds")
                        row["generation_failure_count"] = len(result.get("generation_failures", []))
                        row["status_counts"] = result.get("status_counts", {})
                        best = result.get("best")
                        if best:
                            metrics = best["record"]["metrics"]
                            cache = metrics.get("cache_stats", {})
                            copy_bytes = metrics.get("data_movement_bytes", {})
                            row.update({"makespan": metrics["makespan"],
                                "speedup": 1.0 if n == 1 and p in (1, 2) else baseline[case] / metrics["makespan"],
                                "diagnostic_baseline_over_time": baseline[case] / metrics["makespan"],
                                "added_copy_bytes": copy_bytes.get("added_copy_bytes"),
                                "partition_added_copy_bytes": copy_bytes.get("partition_added_copy_bytes"),
                                "spill_added_copy_bytes": copy_bytes.get("spill_added_copy_bytes"),
                                "cache_byte_hit_rate": cache.get("hit_rate"),
                                "cache_hit_bytes": cache.get("hit_bytes"), "cache_miss_bytes": cache.get("miss_bytes"),
                                "active_cores": metrics.get("active_cores"), "winning_stage": best["stage"],
                                "winning_name": best["name"], "winning_family": best.get("metadata", {}).get("family"),
                                "plan_path": str(attempt / "best.plan.json"), "plan_sha256": object_digest(best["plan"]),
                                "official_result_path": best["record"]["result_path"],
                                "official_result_sha256": best["record"]["result_sha256"]})
                        incumbent = None
                        budget_times = {}
                        for index, trial in enumerate(result.get("evaluations", []), 1):
                            record = trial["record"]
                            if record["status"] == "success":
                                t = record["metrics"]["makespan"]
                                incumbent = t if incumbent is None else min(t, incumbent)
                            budget_times[index] = incumbent
                            trials.append({"batch": directory.name, "case": case, "problem": p, "num_cores": n,
                                "seed": settings["seed"], "logical_call": index, "stage": trial["stage"],
                                "name": trial["name"], "family": trial.get("metadata", {}).get("family"),
                                "status": record["status"], "cache_hit": record.get("cache_hit", False),
                                "makespan": record.get("metrics", {}).get("makespan"), "best_so_far": incumbent,
                                "elapsed_seconds": record["elapsed_seconds"], "record_path": record.get("record_path"),
                                "plan_sha256": trial["plan_sha256"]})
                        for cap in (4, 12, 24):
                            if result["evaluated_count"]:
                                t = budget_times.get(min(cap, result["evaluated_count"]))
                                row[f"time_at_cap_{cap}"] = t
                                row[f"speedup_at_cap_{cap}"] = (None if t is None else
                                    1.0 if n == 1 and p in (1, 2) else baseline[case] / t)
                        for s in result.get("stages", []):
                            stages.append({"batch": directory.name, "case": case, "problem": p, "num_cores": n,
                                "seed": settings["seed"], "stage": s["stage"], "round": s["round"],
                                "before_time": s["before"][0] if s["before"] else None,
                                "after_time": s["after"][0] if s["after"] else None,
                                "evaluations": s["evaluation_end"] - s["evaluation_start"],
                                "generation_seconds": s["generation_seconds"]})
                    except Exception as e:
                        row["status"] = "audit_failed"
                        row["verified_complete"] = False
                        errors.append({"slot": slot, "batch": directory.name, "error": str(e)})
                    rows.append(row)
    groups = defaultdict(list)
    for row in rows:
        groups[row["batch"], row["problem"], row["num_cores"], row["seed"]].append(row)
    aggregates = []
    for key, values in sorted(groups.items()):
        valid = [r for r in values if r["verified_complete"]]
        full = len(values) == len(valid) == 100 and len({r["case"] for r in values}) == 100
        aggregate = {"batch": key[0], "problem": key[1], "num_cores": key[2], "seed": key[3],
                     "planned": len(values), "verified_complete": len(valid), "all100": full,
                     "status_counts": dict(Counter(r["status"] for r in values)),
                     "mean_speedup": None, "median_speedup": None, "minimum_speedup": None}
        if full:
            speeds = [r["speedup"] for r in valid]
            aggregate.update({"mean_speedup": statistics.mean(speeds), "median_speedup": statistics.median(speeds),
                "minimum_speedup": min(speeds), "geometric_mean_speedup": math.exp(statistics.mean(map(math.log, speeds))),
                "mean_logical_calls": statistics.mean(r["evaluations"] for r in valid),
                "total_official_calls": sum(r["official_calls"] for r in valid),
                "total_cache_hits": sum(r["cache_hits"] for r in valid),
                "winning_stages": dict(Counter(r["winning_stage"] for r in valid)),
                "mean_observed_solver_seconds": statistics.mean(r["solver_elapsed_seconds"] for r in valid),
                "walltime_note": "shared exact-plan cache and concurrent graph jobs; not a cold isolated timing comparison"})
            for cap in (4, 12, 24):
                field = f"speedup_at_cap_{cap}"
                aggregate[f"mean_{field}"] = (statistics.mean(r[field] for r in valid)
                                             if all(r.get(field) is not None for r in valid) else None)
        aggregates.append(aggregate)
    out.mkdir(parents=True, exist_ok=True)
    for filename, data in (("all_slots.csv", rows), ("all_trials.csv", trials), ("all_stages.csv", stages), ("aggregates.csv", aggregates)):
        write_csv(out / filename, data)
    summary = {"scope": "frozen formal_v2 runs; 100-case averages require complete verified coverage",
               "batches": len(sources), "planned_slots": len(rows), "verified_complete": sum(r["verified_complete"] for r in rows),
               "audit_errors": errors, "aggregates": aggregates, "sources": sources,
               "source_script_sha256": digest(__file__)}
    atomic_json(out / "summary.json", summary)
    lines = ["# 正式实验审计汇总", "", f"已审计完成 {summary['verified_complete']}/{len(rows)} 个配置。", "",
             "不满 100 图的组仅报告覆盖，不计算伪装为全量的均值。共享精确计划缓存不免除逻辑计费，墙时不能作为独立冷启动效率结论。", "",
             "| 批次 | 问题 | 核数 | 完成 | 平均逐图加速比 |", "|---|---:|---:|---:|---:|"]
    for g in aggregates:
        score = f"{g['mean_speedup']:.6f}" if g["mean_speedup"] is not None else "未齐全"
        lines.append(f"| {g['batch']} | {g['problem']} | {g['num_cores']} | {g['verified_complete']}/{g['planned']} | {score} |")
    lines += ["", f"审计错误数：{len(errors)}。完整逐图、逐次候选和阶段变化见同目录 CSV；源清单见 summary.json。"]
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=RESEARCH / "advanced_solver/runs/formal_v2")
    p.add_argument("--out", type=Path, default=RESEARCH / "实验记录/正式实验汇总_v2")
    p.add_argument("--batches", nargs="*")
    a = p.parse_args()
    result = summarize(a.root, a.out, a.batches)
    print(json.dumps({k: result[k] for k in ("planned_slots", "verified_complete", "audit_errors")}, ensure_ascii=False))
    raise SystemExit(1 if result["audit_errors"] else 0)
