"""Read-only selection/aggregation plus at most 12 fresh official replays."""
from collections import Counter
import gzip
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import time

OUT = Path(__file__).resolve().parent
RESEARCH = OUT.parents[1]
ROOT = RESEARCH.parent
RUNS = RESEARCH / "advanced_solver/runs"
sys.path.insert(0, str(RESEARCH / "solver"))
from common import object_digest
from evaluator import evaluate
from graph_ir import GraphIR
from plan import validate_plan

CASES = ("case_044", "case_049", "case_051", "case_071", "case_082", "case_093")
RUN_NAMES = ("component_probe_v1", "operation_smoke_v1", "operation_all100_n5_v1", "trace_refine_v1", "cache_refine_v1")


def read(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1048576), b""):
            result.update(chunk)
    return result.hexdigest()


def raw(record):
    with gzip.open(record["result_path"], "rt", encoding="utf-8") as handle:
        return json.load(handle)


def metrics_key(record):
    return record["metrics"]["makespan"], record["metrics"].get("data_movement_bytes", {}).get("added_copy_bytes", 0)


def fixed_sources():
    files = list((ROOT / "选题分析/A题附件/code").glob("*.py"))
    files += list((RESEARCH / "solver").glob("*.py"))
    files += [ROOT / "选题分析/A题附件/data/config.txt"]
    files += [ROOT / "选题分析/A题附件/data" / (case + ".json") for case in CASES]
    return {str(path): digest(path) for path in sorted(files)}


def verify_record_source(record):
    assert record["status"] == "success"
    assert digest(record["result_path"]) == record["result_sha256"]
    assert digest(record["graph_path"]) == record["hashes"]["graph_sha256"]
    assert digest(record["config_path"]) == record["hashes"]["config_sha256"]
    assert record["hashes"]["official_py_sha256"] == {
        p.name: digest(p) for p in sorted(Path(record["official_code"]).glob("*.py"))}
    plan = read(record["plan_path"])
    assert object_digest(plan) == record["hashes"]["plan_sha256"]
    validate_plan(GraphIR.from_path(record["graph_path"]), plan)
    official = raw(record)
    for field in ("makespan", "data_movement_bytes", "cache_stats", "num_cores"):
        if field in record["metrics"]:
            assert official[field] == record["metrics"][field], field
    return plan, official


def walk_records(value, pointer=""):
    if isinstance(value, dict):
        if {"status", "metrics", "hashes", "problem", "result_path", "plan_path", "graph_path"} <= set(value):
            yield pointer, value
            return
        for key, item in value.items():
            yield from walk_records(item, pointer + "/" + str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk_records(item, pointer + "/" + str(index))


def collect_pool():
    progress = read(RUNS / "operation_all100_n5_v1/progress.json")
    assert progress["expected"] == 100 and len(progress["completed"]) == 100 and not progress["failures"]
    assert read(RUNS / "component_probe_v1/summary.json")["completed"] is True
    assert (RUNS / "trace_refine_v1/completion.json").exists()
    assert (RUNS / "cache_refine_v1/summary.json").exists()
    pool, source_files = {}, []
    for run_name in RUN_NAMES:
        directory = RUNS / run_name
        files = []
        for name in ("summary.json", "attempts.json", "completion.json"):
            if (directory / name).exists():
                files.append(directory / name)
        files += [p for p in (directory / "results").glob("case_*.json") if not p.name.endswith(".plan.json")]
        files += list(directory.glob("case_*/summary.json"))
        files += list(directory.glob("case_*/all_evaluations.json"))
        files += list(directory.glob("case_*/starting_incumbent.json"))
        files += list(directory.glob("case_*/round_*/all_attempts.json"))
        for path in sorted(set(files)):
            source_files.append({"path": str(path), "sha256": digest(path)})
            for pointer, record in walk_records(read(path)):
                case = Path(record["graph_path"]).stem
                if (case not in CASES or record["status"] != "success" or record["problem"] not in (2, 3)
                        or record["metrics"].get("num_cores") != 5):
                    continue
                key = case, record["problem"], record["hashes"]["plan_sha256"]
                source = {"run": run_name, "path": str(path), "json_pointer": pointer}
                if key in pool:
                    assert metrics_key(pool[key]["record"]) == metrics_key(record), "same exact plan has inconsistent metrics"
                    pool[key]["also_seen_in"].append(source)
                else:
                    pool[key] = {"case": case, "problem": record["problem"], "record": record,
                                 "source": source, "also_seen_in": []}
    return pool, source_files


def summarize_ratios(rows, time_field):
    applicable = [r for r in rows if r[time_field] is not None]
    ratios = [r[time_field] / r["control"] for r in applicable]
    return {"count": len(applicable), "missing": len(rows) - len(applicable),
            "wins": sum(r[time_field] < r["control"] for r in applicable),
            "ties": sum(r[time_field] == r["control"] for r in applicable),
            "losses": sum(r[time_field] > r["control"] for r in applicable),
            "arithmetic_mean_control_over_time": statistics.mean(1 / x for x in ratios),
            "geometric_mean_time_over_control": math.exp(statistics.mean(math.log(x) for x in ratios)),
            "median_time_over_control": statistics.median(ratios),
            "median_control_over_time": statistics.median(1 / x for x in ratios),
            "official_mean_singlecore_over_time": statistics.mean(r["singlecore"] / r[time_field] for r in applicable)}


def campaign_aggregate():
    directory = RUNS / "operation_all100_n5_v1"
    rows, statuses, generation_failures, source_paths = [], Counter(), [], []
    for index in range(1, 101):
        case = "case_{:03d}".format(index)
        path = directory / "results" / (case + ".json")
        item = read(path)
        assert item["case"] == case and item["problem"] == 2 and item["num_cores"] == 5
        control, new, selected = item["control"], item["best_new"], item["selected"]
        successful = [row["record"] for row in item["evaluations"] if row["record"]["status"] == "success"]
        assert (new is None) == (not successful)
        if new:
            assert metrics_key(new["record"]) == min(map(metrics_key, successful))
        assert metrics_key(selected["record"]) == min([metrics_key(control["record"])] + list(map(metrics_key, successful)))
        # Bind aggregates to retained official gzip hashes and exact plan hashes.
        for value in (control, new, selected):
            if value:
                record = value["record"]
                assert digest(record["result_path"]) == record["result_sha256"]
                assert object_digest(value["plan"]) == record["hashes"]["plan_sha256"]
        base_path = RESEARCH / "solver/runs/full_initial_v1/results" / case / "singlecore.json"
        base = read(base_path)
        assert base["status"] == "success" and base["problem"] == 0
        assert base["hashes"]["graph_sha256"] == control["record"]["hashes"]["graph_sha256"]
        statuses.update(row["record"]["status"] for row in item["evaluations"])
        failures = item.get("generation", {}).get("generation_failures", [])
        if failures:
            generation_failures.append({"case": case, "failures": failures})
        rows.append({"case": case, "singlecore": base["metrics"]["makespan"],
                     "control": control["record"]["metrics"]["makespan"],
                     "best_new": new["record"]["metrics"]["makespan"] if new else None,
                     "selected": selected["record"]["metrics"]["makespan"],
                     "candidate_count": len(item["evaluations"]),
                     "control_source": control["source_path"], "singlecore_source": str(base_path),
                     "selected_name": selected["name"], "selected_plan_sha256": selected["record"]["hashes"]["plan_sha256"]})
        source_paths.append({"path": str(path), "sha256": digest(path)})
    result = {"case_count": len(rows), "problem": 2, "num_cores": 5,
              "scope": "all100 operation campaign, historical simple-WCC controls retained; not an equal-budget comparison",
              "official_mean_singlecore_over_control": statistics.mean(r["singlecore"] / r["control"] for r in rows),
              "best_new_vs_control": summarize_ratios(rows, "best_new"),
              "retained_vs_control": summarize_ratios(rows, "selected"),
              "candidate_status_counts": dict(statuses), "generation_failures": generation_failures,
              "candidate_count": sum(statuses.values()), "source_paths": source_paths,
              "rows": rows, "manifest_path": str(directory / "manifest.json"),
              "manifest_sha256": digest(directory / "manifest.json")}
    write(OUT / "campaign_aggregate.json", result)
    return result


def main():
    started = time.monotonic()
    before = fixed_sources()
    advanced_before = {str(p): digest(p) for p in sorted((RESEARCH / "advanced_solver").glob("*.py"))}
    pool, sources = collect_pool()
    selection, missing = [], []
    for case in CASES:
        for problem in (2, 3):
            options = [value for (c, p, _), value in pool.items() if c == case and p == problem]
            if not options:
                missing.append({"case": case, "problem": problem, "reason": "no existing successful N5 result in the five completed advanced runs"})
                continue
            winner = min(options, key=lambda v: (*metrics_key(v["record"]), v["record"]["hashes"]["plan_sha256"]))
            selection.append({**winner, "distinct_successful_plans_considered": len(options)})
    assert len(selection) <= 12
    write(OUT / "selection.json", {"sources": sources, "selected": selection, "missing": missing})
    comparisons = []
    for winner in selection:
        old = winner["record"]
        plan, old_raw = verify_record_source(old)
        fresh_dir = OUT / "fresh" / (winner["case"] + "_p" + str(winner["problem"]))
        assert not fresh_dir.exists(), "fresh replay directory must not already exist"
        record = evaluate(old["graph_path"], plan, winner["problem"], fresh_dir, timeout=120.0,
                          config_path=old["config_path"], official_code=old["official_code"])
        row = {"case": winner["case"], "problem": winner["problem"], "source": winner["source"],
               "old_record": old, "fresh_record": record, "fresh_success": record["status"] == "success",
               "cache_reused": record.get("cache_hit"), "distinct_successful_plans_considered": winner["distinct_successful_plans_considered"]}
        if record["status"] == "success":
            new_raw = raw(record)
            fields = ("makespan", "data_movement_bytes", "cache_stats", "memory_peak_by_core", "num_cores")
            row["metric_equality"] = {f: old_raw.get(f) == new_raw.get(f) for f in fields}
            keys = ("graph_sha256", "plan_sha256", "config_sha256", "official_py_sha256", "wrapper_sha256", "worker_sha256")
            row["hash_equality"] = {key: old["hashes"].get(key) == record["hashes"].get(key) for key in keys}
            row["exact_timeline_equal"] = old_raw.get("per_core_timeline") == new_raw.get("per_core_timeline")
            row["verified"] = all(row["metric_equality"].values()) and all(row["hash_equality"].values()) and not record["cache_hit"]
        else:
            row["verified"] = False
        comparisons.append(row)
        write(OUT / "partial_verification.json", comparisons)
        print(winner["case"], winner["problem"], old["metrics"]["makespan"], record.get("metrics", {}).get("makespan"), record["status"], row["verified"], flush=True)
    aggregate = campaign_aggregate()
    after = fixed_sources()
    advanced_after = {str(p): digest(p) for p in sorted((RESEARCH / "advanced_solver").glob("*.py"))}
    verification = {"scope": "second independent exact-plan verification, not further optimization",
                    "cases": list(CASES), "max_calls": 12, "calls": len(comparisons), "workers": 1,
                    "timeout_seconds": 120, "all_fresh_verified": all(row["verified"] for row in comparisons),
                    "comparisons": comparisons, "missing_scenarios": missing,
                    "source_files": sources, "input_hashes_before": before, "input_hashes_after": after,
                    "official_solver_graph_config_unchanged": before == after,
                    "advanced_module_hashes_before": advanced_before, "advanced_module_hashes_after": advanced_after,
                    "advanced_module_changes": [p for p in set(advanced_before) | set(advanced_after) if advanced_before.get(p) != advanced_after.get(p)],
                    "campaign_aggregate_path": str(OUT / "campaign_aggregate.json"),
                    "aggregate_summary": {k: v for k, v in aggregate.items() if k not in ("rows", "source_paths")},
                    "elapsed_seconds": time.monotonic() - started}
    write(OUT / "verification.json", verification)
    print("DONE", verification["calls"], verification["all_fresh_verified"], "unchanged", before == after, flush=True)


if __name__ == "__main__":
    main()
