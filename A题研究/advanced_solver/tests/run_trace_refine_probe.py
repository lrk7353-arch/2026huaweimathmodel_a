#!/usr/bin/env python3
"""Frozen four-case iterative P2/N5 validation, at most 40 calls, one worker.

No test-case-dependent placement rule: cases only choose frozen starting records.
Two rounds, at most 6 then 4 unique official evaluations per case; 60s/call and
300s evaluation-phase budget. Generation/hash verification excluded from timer.
"""
import argparse
from collections import Counter
import csv
import gzip
import json
from pathlib import Path
import shutil
import sys
import time

sys.dont_write_bytecode = True
RESEARCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH / "solver"))
from solver.common import DATA, OFFICIAL, atomic_json, digest, object_digest, read_json
from solver.graph_ir import GraphIR
from solver.evaluator import evaluate
from advanced_solver.trace_refine import generate_trace_candidates

MODULE = RESEARCH / "advanced_solver/trace_refine.py"


def key(entry):
    m = entry["record"]["metrics"]
    return m["makespan"], m["data_movement_bytes"]["added_copy_bytes"]


def checked(ir, entry):
    record = entry["record"]
    if record["status"] != "success" or record["problem"] != 2:
        raise ValueError("starting record must be successful P2")
    hashes = record["hashes"]
    expected = {p.name: digest(p) for p in sorted(OFFICIAL.glob("*.py"))}
    if hashes["graph_sha256"] != digest(ir.path) or hashes["config_sha256"] != digest(DATA / "config.txt"):
        raise ValueError("starting input/config changed")
    if hashes["official_py_sha256"] != expected or digest(record["result_path"]) != record["result_sha256"]:
        raise ValueError("official source/result changed")
    if digest(record["plan_path"]) != hashes["plan_sha256"]:
        raise ValueError("saved plan changed")
    plan = read_json(record["plan_path"])
    if "plan" in entry and object_digest(plan) != object_digest(entry["plan"]):
        raise ValueError("entry plan and officially evaluated plan differ")
    with gzip.open(record["result_path"], "rt", encoding="utf-8") as stream:
        official = json.load(stream)
    if official["makespan"] != record["metrics"]["makespan"]:
        raise ValueError("saved makespan differs")
    return {**entry, "plan": plan}, official


def frozen_inputs():
    result = []
    for case in ("case_071", "case_044", "case_049", "case_069"):
        if case in ("case_071", "case_044"):
            path = RESEARCH / "advanced_solver/runs/operation_smoke_v1/results" / (case + ".json")
            entry = read_json(path)["selected"]
            source = "operation_smoke_v1"
        elif case == "case_049":
            path = RESEARCH / "探索/runs/critical_transfer_v1/summary.json"
            entry = next(c["selected"] for c in read_json(path)["cases"] if c["case"] == case)
            source = "critical_transfer_v1"
        else:
            path = RESEARCH / "solver/runs/full_initial_v1/results" / case / "simple_p2_n5_seed0.json"
            entry = read_json(path)["best"]
            source = "full_initial_v1_simple"
        ir = GraphIR.from_path(DATA / (case + ".json"))
        incumbent, official = checked(ir, entry)
        result.append((ir, incumbent, official, {"source": source, "source_path": str(path), "sha256": digest(path)}))
    return result


def save(out, cases, attempts, eval_seconds, exhausted):
    atomic_json(out / "summary.json", {"scope": "four frozen cases, P2/N5; two rounds, max40 calls",
        "cases": cases, "attempts": attempts, "evaluation_seconds": eval_seconds,
        "evaluation_budget_seconds": 300, "budget_exhausted": exhausted})
    fields = ["case", "round", "candidate", "family", "status", "makespan", "added_copy_bytes", "elapsed_seconds", "record_path"]
    with (out / "attempts.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(attempts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=RESEARCH / "advanced_solver/runs/trace_refine_v1")
    args = parser.parse_args()
    out = args.run_dir.resolve()
    if DATA.parent.resolve() == out or DATA.parent.resolve() in out.parents or (out.exists() and any(out.iterdir())):
        parser.error("run directory must be new/empty and outside official attachments")
    out.mkdir(parents=True, exist_ok=True)
    sources = [MODULE, Path(__file__).resolve()]
    sources.extend(p for base in (RESEARCH / "solver", OFFICIAL) for p in sorted(base.glob("*.py")))
    hashes = {str(p): digest(p) for p in sources}
    for p in sources:
        target = out / "source_snapshot" / p.relative_to(RESEARCH.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, target)
    datasets = frozen_inputs()
    manifest = {"source_sha256": hashes, "config_sha256": digest(DATA / "config.txt"),
                "cases": [{"case": ir.path.stem, "baseline": key(inc), **source} for ir, inc, _, source in datasets],
                "round_call_budgets": [6, 4], "max_calls": 40, "timeout_per_call": 60,
                "evaluation_seconds_budget": 300, "workers": 1,
                "selection": "successful official makespan, then added_copy_bytes; original incumbent retained"}
    atomic_json(out / "manifest.json", manifest)
    attempts, cases, eval_seconds, exhausted = [], [], 0.0, False
    for ir, starting, official, source in datasets:
        case_dir = out / ir.path.stem
        atomic_json(case_dir / "starting_incumbent.json", {**starting, "provenance": source})
        incumbent, seen = starting, {object_digest(starting["plan"])}
        round_rows = []
        for round_index, calls in enumerate((6, 4)):
            gen_started = time.monotonic()
            candidates, diagnostics = generate_trace_candidates(ir, incumbent["plan"], official,
                num_cores=5, max_candidates=12, round_index=round_index, seed=0)
            generation_seconds = time.monotonic() - gen_started
            round_dir = case_dir / ("round_{}".format(round_index))
            atomic_json(round_dir / "generation.json", {"diagnostics": diagnostics, "generation_seconds": generation_seconds,
                                                        "candidates": [{k: v for k, v in c.items() if k != "plan"} for c in candidates]})
            baseline_key, rows, called = key(incumbent), [], 0
            round_best = incumbent
            for candidate in candidates:
                signature = object_digest(candidate["plan"])
                atomic_json(round_dir / "plans" / (candidate["name"] + ".json"), candidate["plan"])
                reason = None
                if signature in seen:
                    reason = "previously_attempted_plan"
                elif called >= calls:
                    reason = "round_call_budget"
                elif len(attempts) >= 40 or eval_seconds >= 298:
                    reason, exhausted = "global_evaluation_budget", True
                if reason:
                    rows.append({"candidate": candidate["name"], "metadata": candidate["metadata"], "status": "not_evaluated", "reason": reason})
                    continue
                seen.add(signature)
                called += 1
                evaluation_started = time.monotonic()
                record = evaluate(ir.path, candidate["plan"], 2, out / "evaluations",
                                  timeout=min(60, 298 - eval_seconds), config_path=DATA / "config.txt")
                eval_seconds += time.monotonic() - evaluation_started
                row = {"candidate": candidate["name"], "metadata": candidate["metadata"], "record": record,
                       "plan": candidate["plan"]}
                rows.append(row)
                if record["status"] == "success" and key(row) < key(round_best):
                    round_best = row
                m = record.get("metrics", {})
                attempts.append({"case": ir.path.stem, "round": round_index, "candidate": candidate["name"],
                    "family": candidate["metadata"]["family"], "status": record["status"], "makespan": m.get("makespan"),
                    "added_copy_bytes": m.get("data_movement_bytes", {}).get("added_copy_bytes"),
                    "elapsed_seconds": record["elapsed_seconds"], "record_path": record.get("record_path")})
                atomic_json(round_dir / "all_attempts.json", rows)
                print(json.dumps(attempts[-1]), flush=True)
            atomic_json(round_dir / "all_attempts.json", rows)
            incumbent, official = checked(ir, round_best)
            round_rows.append({"round": round_index, "starting_metrics": baseline_key, "selected_metrics": key(incumbent),
                               "called": called, "kept_previous": key(incumbent) == baseline_key})
            atomic_json(round_dir / "selected.plan.json", incumbent["plan"])
        cases.append({"case": ir.path.stem, "starting_source": source, "starting_metrics": key(starting),
                      "selected_metrics": key(incumbent), "rounds": round_rows, "selected": incumbent,
                      "improved_makespan": key(incumbent)[0] < key(starting)[0],
                      "relative_makespan_reduction_pct": 100 * (key(starting)[0] - key(incumbent)[0]) / key(starting)[0]})
        atomic_json(case_dir / "selected.plan.json", incumbent["plan"])
        save(out, cases, attempts, eval_seconds, exhausted)
    unchanged = hashes == {str(p): digest(p) for p in sources}
    atomic_json(out / "completion.json", {"source_hashes_unchanged": unchanged, "calls": len(attempts),
                "statuses": dict(Counter(a["status"] for a in attempts)), "evaluation_seconds": eval_seconds,
                "budget_exhausted": exhausted})
    if not unchanged:
        raise RuntimeError("source changed during bounded validation")


if __name__ == "__main__":
    main()
