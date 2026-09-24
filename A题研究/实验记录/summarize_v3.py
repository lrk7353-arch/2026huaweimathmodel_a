#!/usr/bin/env python3
"""Verified selected-result statistics for frozen v3 batches, never partial means."""
from collections import defaultdict
import argparse
from pathlib import Path
import statistics
import sys
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
sys.path[:0] = [str(RESEARCH / "精修求解器"), str(RESEARCH), str(HERE)]
import controller
from solver.common import read_json, atomic_json, digest, object_digest
from solver.graph_ir import GraphIR
from summarize_formal import baselines, write_csv


def summarize(root, output):
    baseline, rows, groups, errors, input_manifests = baselines(), [], defaultdict(list), [], []
    sources = controller.source_hashes()
    for directory in sorted(Path(root).iterdir()):
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = read_json(manifest_path)
        if manifest.get("batch_protocol") != "fresh_v3_portfolio":
            continue
        if manifest["source_sha256"] != sources:
            raise ValueError("v3 source manifest changed")
        settings = manifest["settings"]
        config = Path(settings["config"])
        if digest(config) != manifest["config_sha256"]:
            raise ValueError("v3 configuration changed")
        report_path = directory / "summary.json"
        if not report_path.exists():
            report_path = directory / "progress.json"
        reported = {r["slot"]: r for r in read_json(report_path)["slots"]} if report_path.exists() else {}
        input_manifests.append({"path": str(manifest_path), "sha256": digest(manifest_path),
                                "report_path": str(report_path), "report_sha256": digest(report_path) if report_path.exists() else None})
        for case_number in settings["cases"]:
            case = f"case_{case_number:03d}"
            graph = Path(settings["data_dir"]) / (case + ".json")
            if digest(graph) != manifest["graphs_sha256"][case]:
                raise ValueError("v3 graph changed")
            ir = None
            for problem in settings["problems"]:
                for cores in settings["cores"]:
                    slot = f"{case}_p{problem}_n{cores}"
                    report = reported.get(slot, {})
                    row = {"batch": directory.name, "case": case, "problem": problem, "num_cores": cores,
                           "seed": settings["seed"], "status": report.get("outcome", "pending"),
                           "verified": False, "baseline_makespan": baseline[case], "logical_cap": settings["max_evaluations"]}
                    if report.get("feasible") and report.get("search_completed"):
                        try:
                            summary_path = Path(report["summary_path"])
                            if digest(summary_path) != report["summary_sha256"]:
                                raise ValueError("batch verified summary SHA changed")
                            s = read_json(summary_path)
                            if not s["completed"] or s["status"] != "success" or s.get("requires_review") or not s["source_and_input_hashes_verified"]:
                                raise ValueError("slot incomplete or needs review")
                            if (s["problem"], s["num_cores"], s["seed"], s["source_sha256"]) != (problem, cores, settings["seed"], sources):
                                raise ValueError("slot source/settings mismatch")
                            if s["graph_sha256"] != manifest["graphs_sha256"][case] or s["config_sha256"] != manifest["config_sha256"]:
                                raise ValueError("slot input manifest mismatch")
                            best = s["best"]
                            if digest(s["output"]) != s["output_sha256"] or object_digest(read_json(s["output"])) != best["plan_sha256"]:
                                raise ValueError("exported plan changed")
                            if ir is None:
                                ir = GraphIR.from_path(graph)
                            context = SimpleNamespace(ir=ir, manifest=s, problem=problem, num_cores=cores, graph_path=graph)
                            controller.Solver.verify_success(context, best["record"], best["plan"], best["plan_sha256"])
                            metrics = best["record"]["metrics"]
                            row.update(verified=True, makespan=metrics["makespan"],
                                official_speedup=1.0 if problem in (1, 2) and cores == 1 else baseline[case] / metrics["makespan"],
                                diagnostic_baseline_over_time=baseline[case] / metrics["makespan"],
                                added_copy_bytes=metrics["data_movement_bytes"]["added_copy_bytes"],
                                spill_copy_bytes=metrics["data_movement_bytes"].get("spill_added_copy_bytes"),
                                cache_byte_hit_rate=metrics.get("cache_stats", {}).get("hit_rate"),
                                active_cores=sum(bool(seq) for seq in best["plan"]["core_schedules"]),
                                logical_calls=s["logical_calls"], cache_hits=s["cache_hits"],
                                stage_calls=s["stage_calls"], status_counts=s["status_counts"],
                                winning_stage=best["stage"], winning_name=best["name"],
                                selected_plan_sha256=best["plan_sha256"], summary_sha256=digest(summary_path))
                            incumbent = None
                            prefix = {}
                            for index, trial in enumerate(s["evaluations"], 1):
                                if trial["record"]["status"] == "success":
                                    value = controller.objective(trial["record"])[0]
                                    incumbent = min(value, incumbent) if incumbent is not None else value
                                prefix[index] = incumbent
                            for cap in (4, 12, 24, 48, 90):
                                row[f"prefix_{cap}_makespan"] = prefix.get(min(cap, len(prefix)))
                        except (ValueError, KeyError, OSError) as error:
                            row.update(status="audit_failed", error=str(error), verified=False)
                            errors.append({"slot": slot, "batch": directory.name, "error": str(error)})
                    rows.append(row)
                    groups[directory.name, problem, cores, settings["seed"]].append(row)
    aggregates = []
    for (batch, problem, cores, seed), values in sorted(groups.items()):
        complete = len(values) == 100 and {v["case"] for v in values} == set(baseline) and all(v["verified"] for v in values)
        aggregates.append({"batch": batch, "problem": problem, "num_cores": cores, "seed": seed,
                           "reported_verified": sum(v["verified"] for v in values), "planned": len(values),
                           "all_100": complete,
                           "mean_official_speedup": statistics.mean(v["official_speedup"] for v in values) if complete else None,
                           "mean_logical_calls": statistics.mean(v["logical_calls"] for v in values) if complete else None})
    output = Path(output)
    write_csv(output / "all_slots.csv", rows)
    write_csv(output / "aggregates.csv", aggregates)
    result = {"scope": "v3 reinforced budget90; separate from v2 equal-budget ablations",
              "source_sha256": sources, "summary_script_sha256": digest(__file__), "input_manifests": input_manifests,
              "selected_evidence_verified": "raw winner including full compute/core/timeline binding; immutable batch summary SHA binds previously audited call ledger",
              "slot_count": len(rows), "verified_slots": sum(r["verified"] for r in rows),
              "audit_errors": errors, "partial_group_means_suppressed": True, "aggregates": aggregates}
    atomic_json(output / "summary.json", result)
    if errors:
        raise ValueError("v3 summary encountered evidence audit errors")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=RESEARCH / "精修求解器/runs/formal_v3")
    p.add_argument("--output", type=Path, default=HERE / "强化实验汇总_v3")
    a = p.parse_args()
    result = summarize(a.root, a.output)
    print({"slot_count": result["slot_count"], "verified_slots": result["verified_slots"], "errors": len(result["audit_errors"])})
