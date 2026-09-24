#!/usr/bin/env python3
"""Independent fresh replay of all six frozen P1 probe selections."""
import gzip
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from solver.common import DATA, atomic_json, read_json, digest, object_digest
from solver.evaluator import evaluate
from advanced_solver.engine import source_hashes


def raw(record):
    if record["status"] != "success" or digest(record["result_path"]) != record["result_sha256"]:
        raise ValueError("successful unchanged raw official result required")
    with gzip.open(record["result_path"], "rt", encoding="utf-8") as stream:
        return json.load(stream)


def main():
    source = HERE / "p1_selective_v1/summary.json"
    out = HERE / "p1_selective_v1/independent_replay"
    if out.exists():
        raise ValueError("fresh independent directory required")
    summary = read_json(source)
    if not summary["completed"] or not summary["sources_and_inputs_unchanged"]:
        raise ValueError("probe did not finish cleanly")
    selections = [{"case": r["case"], "plan": r["best"]["plan"], "record": r["best"]["record"]}
                  for r in summary["results"]]
    sources = {**source_hashes(), str(Path(__file__).resolve()): digest(__file__)}
    manifest = {"scope": "all six probe winners including two unchanged controls; exact replay, not held-out performance",
                "source_summary": str(source), "source_summary_sha256": digest(source),
                "source_sha256": sources, "selections": selections, "logical_cap": len(selections),
                "workers": 1, "timeout_seconds": 180, "fresh_cache_required": True}
    atomic_json(out / "manifest.json", manifest)
    rows = []
    for item in selections:
        old = raw(item["record"])
        plan = item["plan"]
        if item["record"]["hashes"]["plan_sha256"] != object_digest(plan):
            raise ValueError("selected plan differs from original evaluated bytes")
        atomic_json(out / item["case"] / "pending.json", item)
        record = evaluate(DATA / (item["case"] + ".json"), plan, 1, out / "evaluations", timeout=180)
        value = {"case": item["case"], "record": record, "fresh": not record.get("cache_hit"),
                 "all_raw_fields_equal": False, "different_raw_fields": []}
        if record["status"] == "success":
            actual = raw(record)
            value["all_raw_fields_equal"] = old == actual
            value["different_raw_fields"] = [k for k in set(old) | set(actual) if old.get(k) != actual.get(k)]
            value["input_hashes_equal"] = all(record["hashes"][k] == item["record"]["hashes"][k]
                for k in ("plan_sha256", "graph_sha256", "config_sha256", "official_py_sha256", "wrapper_sha256", "worker_sha256"))
        rows.append(value)
        atomic_json(out / item["case"] / "verification.json", value)
        print(json.dumps({"case": item["case"], "status": record["status"],
                          "all_raw_fields_equal": value["all_raw_fields_equal"]}), flush=True)
    value = {"calls": len(rows), "all_fresh": all(r["fresh"] for r in rows),
             "all_verified": all(r["all_raw_fields_equal"] and r.get("input_hashes_equal") for r in rows),
             "sources_unchanged": all(digest(p) == h for p, h in sources.items()),
             "frozen_selection_unchanged": digest(source) == manifest["source_summary_sha256"], "results": rows}
    atomic_json(out / "verification.json", value)
    print(json.dumps({k: v for k, v in value.items() if k != "results"}), flush=True)
    if not all(value[k] for k in ("all_fresh", "all_verified", "sources_unchanged", "frozen_selection_unchanged")):
        raise RuntimeError("independent replay has a verification failure")


if __name__ == "__main__":
    main()
