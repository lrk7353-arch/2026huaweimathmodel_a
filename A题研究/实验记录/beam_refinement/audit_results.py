#!/usr/bin/env python3
"""Read-only verification of beam evidence; writes a separate audit report."""
import argparse
import csv
import gzip
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trace_beam import atomic_json, digest, object_digest, read_json, score, ancestry


def audit(out):
    out = Path(out).resolve()
    manifest = read_json(out / "manifest.json")
    completion = read_json(out / "completion.json")
    summary = read_json(out / "summary.json")
    assert completion["completed"] and completion["source_hashes_unchanged"]
    assert all(digest(p) == h for p, h in manifest["source_sha256"].items())
    assert digest(out / "protocol.json") == manifest["protocol_sha256"]
    protocol = manifest["protocol"]
    assert [c["case"] for c in summary["cases"]] == protocol["cases"]
    rows, verified_records, unique_results = [], 0, set()
    total_logical = total_official = 0
    for case_row in summary["cases"]:
        case = case_row["case"]
        initial = read_json(out / case / "initial.json")
        provenance = initial["provenance"]
        assert digest(provenance["selected_source"]) == provenance["source_sha256"]
        assert all(digest(item["source"]) == item["sha256"] for item in provenance["options"])
        assert digest(initial["record"]["result_path"]) == provenance["result_sha256"]
        assert digest(initial["record"]["plan_path"]) == provenance["plan_file_sha256"]
        results = {}
        for strategy, width, cap in (("greedy", 1, protocol["greedy_logical_cap"]), ("beam", protocol["beam_width"], protocol["beam_logical_cap"])):
            r = read_json(out / case / strategy / "result.json")
            results[strategy] = r
            assert r["logical_trials"] == len(r["trials"]) <= cap
            assert len({t["key"] for t in r["trials"]}) == r["logical_trials"]
            current = {"key": initial["key"], "metrics": initial["metrics"]}
            assert r["best_by_prefix"][0] == current
            for index, trial in enumerate(r["trials"], 1):
                record = trial["record"]
                assert trial["logical_index"] == index and trial["parent"] in r["nodes"]
                if record["status"] == "success":
                    assert record["problem"] == 2
                    assert digest(record["plan_path"]) == record["hashes"]["plan_sha256"]
                    plan = read_json(record["plan_path"])
                    assert object_digest(plan) == trial["key"]
                    assert digest(record["result_path"]) == record["result_sha256"]
                    with gzip.open(record["result_path"], "rt", encoding="utf-8") as f:
                        official = json.load(f)
                    assert official["makespan"] == score(record)[0] and official["num_cores"] == 5
                    unique_results.add(record["result_path"])
                    verified_records += 1
                    if list(score(record)) < current["metrics"]:
                        current = {"key": trial["key"], "metrics": list(score(record))}
                assert r["best_by_prefix"][index] == current
            assert r["best"]["key"] == current["key"]
            for layer in r["layers"]:
                assert len(layer["next_frontier"]) <= width
                assert layer["threshold_makespan"] == layer["global_after"][0] * (1 + protocol["relative_makespan_slack"])
                for key in layer["next_frontier"]:
                    node = r["nodes"][key]
                    assert node["metrics"][0] <= layer["threshold_makespan"]
                    assert node["layer"] <= layer["layer"]
            total_logical += r["logical_trials"]
            total_official += r["official_calls"]
        comparison = case_row["comparison"]
        m = min(protocol["matched_comparison_cap"], results["greedy"]["logical_trials"], results["beam"]["logical_trials"])
        assert comparison["common_logical_prefix"] == m
        gm = results["greedy"]["best_by_prefix"][m] if m else None
        bm = results["beam"]["best_by_prefix"][m] if m else None
        assert gm == comparison["matched_greedy"] and bm == comparison["matched_beam"]
        path = ancestry(results["beam"], results["beam"]["best"]["key"])["path"]
        has_time_worsening = any(b["metrics"][0] > a["metrics"][0] for a, b in zip(path, path[1:]))
        selected_path = out / case / "selected.plan.json"
        selected = read_json(selected_path)
        assert object_digest(selected) == case_row["selected"]["key"]
        strategy = "greedy" if results["greedy"]["best"]["metrics"] <= results["beam"]["best"]["metrics"] else "beam"
        assert case_row["selected"]["metrics"] == results[strategy]["best"]["metrics"]
        rows.append({"case": case, "initial_makespan": initial["metrics"][0], "common_prefix": m,
            "greedy_at_m": gm["metrics"][0] if gm else None, "beam_at_m": bm["metrics"][0] if bm else None,
            "matched_result": comparison["matched_result"], "greedy_calls": results["greedy"]["logical_trials"],
            "beam_calls": results["beam"]["logical_trials"], "greedy_final": results["greedy"]["best"]["metrics"][0],
            "beam_final": results["beam"]["best"]["metrics"][0], "selected_strategy": strategy,
            "selected_makespan": case_row["selected"]["metrics"][0], "selected_added_copy_bytes": case_row["selected"]["metrics"][1],
            "beam_final_path_strictly_worsens_makespan": has_time_worsening,
            "selected_plan": str(selected_path), "selected_plan_file_sha256": digest(selected_path),
            "selected_plan_object_sha256": object_digest(selected)})
    assert total_logical == summary["logical_calls"] == completion["logical_calls"] <= protocol["all_cases_total_logical_cap"]
    assert total_official == summary["official_calls"] == completion["official_calls"]
    result = {"audit_script_sha256": digest(Path(__file__)), "additional_official_calls": 0,
              "verified_success_record_occurrences": verified_records, "unique_success_gzip_paths": len(unique_results),
              "logical_calls": total_logical, "official_calls": total_official,
              "source_and_protocol_still_match": True, "cases": rows}
    atomic_json(out / "audit.json", result)
    with (out / "comparisons.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    result = audit(args.run_dir)
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, ensure_ascii=False))
