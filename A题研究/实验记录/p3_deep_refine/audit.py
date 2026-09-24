"""Independent post-run bookkeeping and exact-result audit; never evaluates."""
from collections import Counter
import csv
import gzip
import hashlib
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def plan_hash(plan):
    return hashlib.sha256(json.dumps(plan, ensure_ascii=False, allow_nan=False,
                                    separators=(",", ":")).encode()).hexdigest()


def key(record):
    return record["metrics"]["makespan"], record["metrics"]["data_movement_bytes"]["added_copy_bytes"]


def main():
    manifest, reported = read(OUT / "manifest.json"), read(OUT / "summary.json")
    cases, totals, total_cache_hits, total_calls = [], Counter(), 0, 0
    raw_verified = 0
    logged_attempts = {}
    for item in manifest["selection"]:
        directory = OUT / item["case"]
        summary, calls = read(directory / "summary.json"), read(directory / "all_calls.json")
        assert len(calls) <= manifest["logical_cap_per_case_all_scenes"]
        assert [r["logical_index"] for r in calls] == list(range(1, len(calls) + 1))
        identities = [(r["problem"], r["plan_sha256"]) for r in calls]
        assert len(set(identities)) == len(identities)
        statuses = Counter(r["record"]["status"] for r in calls)
        cache_hits = sum(bool(r["record"]["cache_hit"]) for r in calls)
        totals.update(statuses); total_cache_hits += cache_hits; total_calls += len(calls)
        for call in calls:
            record = call["record"]
            assert record["attempt_id"] not in logged_attempts
            logged_attempts[record["attempt_id"]] = record
            assert read(record["record_path"]) == record
            assert call["plan_sha256"] == plan_hash(call["plan"]) == record["hashes"]["plan_sha256"]
            assert record["hashes"]["graph_sha256"] == sha(item["graph_path"])
            if call["phase"] in ("reencoding_control", "directed_cache"):
                meta = call["metadata"]
                assert (call["phase"] == "reencoding_control") == meta["is_reencoding_control"]
                if call["phase"] == "reencoding_control":
                    assert meta["target"] is None and meta["changed_core_count"] == 0
            if record["status"] == "success":
                assert sha(record["result_path"]) == record["result_sha256"]
                with gzip.open(record["result_path"], "rt", encoding="utf-8") as handle:
                    official = json.load(handle)
                for field in ("makespan", "num_cores", "data_movement_bytes", "cache_stats"):
                    if field in record["metrics"]:
                        assert official[field] == record["metrics"][field]
                raw_verified += 1
        for path in directory.glob("round_*_generation.json"):
            generation = read(path)
            assert generation["round"] < manifest["max_rounds"]
            assert len(generation["candidates"]) <= manifest["max_candidates_per_round"]
        row = {"case": item["case"], "case_status": summary["status"], "logical_calls": len(calls),
               "p3_calls": sum(r["problem"] == 3 for r in calls), "p2_calls": sum(r["problem"] == 2 for r in calls),
               "cache_hits": cache_hits, "new_worker_calls": len(calls) - cache_hits,
               "failure_count": sum(count for status, count in statuses.items() if status != "success")}
        if summary["status"] == "success":
            selected, initial = summary["selected"]["record"], summary["initialized"]["record"]
            eligible = [r["record"] for r in calls if r["problem"] == 3 and r["record"]["status"] == "success"]
            assert key(selected) == min(map(key, eligible))
            assert plan_hash(read(directory / "selected.plan.json")) == selected["hashes"]["plan_sha256"]
            assert key(selected) <= key(initial)
            family_decreases = summary["accepted_path_time_decreases_by_family_not_causal_effects"]
            assert sum(family_decreases.values()) == key(initial)[0] - key(selected)[0]
            cells = summary["four_cells"]
            assert cells["t2_pi2"]["hashes"]["plan_sha256"] == cells["t3_pi2"]["hashes"]["plan_sha256"]
            assert cells["t2_pi3"]["hashes"]["plan_sha256"] == cells["t3_pi3"]["hashes"]["plan_sha256"]
            complete = all(r["status"] == "success" for r in cells.values())
            row.update({"frozen_p2_makespan": key(item["frozen_p2"]["record"])[0],
                        "frozen_existing_p3_makespan": key(item["frozen_existing_p3"]["record"])[0],
                        "p2_plan_in_p3_makespan": cells["t3_pi2"]["metrics"].get("makespan"),
                        "initialized_p3_makespan": key(initial)[0],
                        "round0_control_candidate_makespan": summary["control_only_round0"]["record"]["metrics"].get("makespan") if summary["control_only_round0"] else None,
                        "round0_control_retained_makespan": key(summary["control_only_retained"]["record"])[0],
                        "final_p3_makespan": key(selected)[0], "cycles_saved_from_initialization": key(initial)[0] - key(selected)[0],
                        "cycles_saved_beyond_round0_control": summary["time_saved_beyond_round0_control"],
                        "initial_added_copy_bytes": key(initial)[1], "final_added_copy_bytes": key(selected)[1],
                        "final_P2_makespan": cells["t2_pi3"]["metrics"].get("makespan"),
                        "four_cell_complete": complete, "final_family": summary["selected_final_family"],
                        "accepted_path_control_decrease": family_decreases["reencoding_control"],
                        "accepted_path_directed_decrease": family_decreases["directed_cache"],
                        "generation_error_count": len(summary["generation_errors"]),
                        "selected_plan_path": str(directory / "selected.plan.json"),
                        "selected_plan_sha256": selected["hashes"]["plan_sha256"],
                        "selected_record_path": selected["record_path"]})
        else:
            row["four_cell_complete"] = False
        cases.append(row)
    assert total_calls <= manifest["total_logical_cap"]
    assert total_calls == reported["logical_calls"]
    attempt_dirs = sorted((OUT / "evaluations/attempts").iterdir())
    attempt_ids = {path.name for path in attempt_dirs}
    missing_records = [str(path) for path in attempt_dirs if not (path / "record.json").exists()]
    unlogged_attempts = sorted(attempt_ids - set(logged_attempts))
    logged_without_attempt = sorted(set(logged_attempts) - attempt_ids)
    assert not missing_records and not unlogged_attempts and not logged_without_attempt
    assert len(attempt_dirs) == total_calls
    assert all(sha(source["snapshot_path"]) == source["sha256"] for source in manifest["source_snapshots"])
    audit = {"scope": "independent audit from every all_calls record, including any initialization failures",
             "cases": cases, "logical_calls": total_calls, "status_counts": dict(totals),
             "cache_hits": total_cache_hits, "new_worker_calls": total_calls - total_cache_hits,
             "successful_raw_gzip_results_verified": raw_verified,
             "wrapper_attempt_directories": len(attempt_dirs),
             "attempts_missing_final_record": missing_records,
             "unlogged_wrapper_attempts": unlogged_attempts,
             "logged_calls_without_attempt": logged_without_attempt,
             "all_four_cells_complete": all(r["four_cell_complete"] for r in cases),
             "fixed_hashes_unchanged": all(sha(path) == value for path, value in manifest["fixed_hashes"].items()),
             "source_snapshots_valid": True, "no_evaluation_performed": True,
             "runner_failure_branch_note": "runner early initialization-failure branch omits summary counters; this independent audit counts all_calls directly and flags incomplete final P2 separately"}
    (OUT / "audit_summary.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    columns = list(dict.fromkeys(key for row in cases for key in row))
    with (OUT / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader(); writer.writerows(cases)
    print(json.dumps({key: value for key, value in audit.items() if key not in ("cases", "runner_failure_branch_note")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
