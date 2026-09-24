"""Pre-register and run the fixed five-case WCC interleaving mechanism probe."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import shutil
import sys

sys.dont_write_bytecode = True
RESEARCH = Path(__file__).resolve().parents[1]
ROOT = RESEARCH.parent
sys.path.insert(0, str(RESEARCH / "solver"))
sys.path.insert(0, str(RESEARCH / "精修求解器"))
from common import atomic_json, digest, object_digest, DATA, OFFICIAL
from evaluator import evaluate
from graph_ir import GraphIR
from wcc_interleave import generate_interleave_candidates

CASES = ("008", "004", "010", "019", "093")


def read(p):
    return json.loads(Path(p).read_text())


def key(record):
    m = record["metrics"]
    return m["makespan"], m["data_movement_bytes"]["added_copy_bytes"]


def prepare(out):
    if out.exists() and any(out.iterdir()):
        raise ValueError("prepare requires a new empty directory")
    out.mkdir(parents=True, exist_ok=True)
    source_files = [Path(__file__), RESEARCH / "精修求解器/wcc_interleave.py",
        RESEARCH / "精修求解器/README_wcc_interleave.md",
        RESEARCH / "精修求解器/tests/test_wcc_interleave.py"]
    source_files += sorted((RESEARCH / "solver").glob("*.py")) + sorted(OFFICIAL.glob("*.py"))
    cases, inputs = [], {str(DATA / "config.txt"): digest(DATA / "config.txt")}
    for case in CASES:
        name = "case_" + case
        old_dir = RESEARCH / "solver/runs/full_initial_v1/results" / name
        old_summary = old_dir / "simple_p2_n5_seed0.json"
        old_plan = old_dir / "simple_p2_n5_seed0.plan.json"
        original, plan = read(old_summary), read(old_plan)
        record = original["best"]["record"]
        graph = DATA / (name + ".json")
        if record["status"] != "success":
            raise ValueError("historical baseline is not successful")
        if object_digest(plan) != record["hashes"]["plan_sha256"]:
            raise ValueError("historical selected plan hash differs from its record")
        if digest(graph) != record["hashes"]["graph_sha256"] or digest(DATA / "config.txt") != record["hashes"]["config_sha256"]:
            raise ValueError("historical graph/config mismatch")
        if any(digest(OFFICIAL / n) != h for n, h in record["hashes"]["official_py_sha256"].items()):
            raise ValueError("historical official code mismatch")
        if digest(record["result_path"]) != record["result_sha256"]:
            raise ValueError("historical official result hash mismatch")
        with gzip.open(record["result_path"], "rt") as f:
            raw = json.load(f)
        if raw["makespan"] != key(record)[0]:
            raise ValueError("historical makespan differs from official output")
        ir = GraphIR.from_path(graph)
        candidates, diagnostics = generate_interleave_candidates(ir, plan, num_cores=5, max_candidates=9, seed=17)
        directory = out / name
        directory.mkdir()
        atomic_json(directory / "baseline.plan.json", plan)
        atomic_json(directory / "baseline.record.json", record)
        items = []
        for i, candidate in enumerate(candidates):
            target = directory / f"candidate_{i:02d}.plan.json"
            atomic_json(target, candidate["plan"])
            items.append({"index": i, "name": candidate["name"], "metadata": candidate["metadata"],
                "plan_path": str(target), "plan_sha256": digest(target)})
        cases.append({"case": name, "graph_path": str(graph), "baseline_summary_path": str(old_summary),
            "baseline_record": record, "baseline_plan_path": str(directory / "baseline.plan.json"),
            "baseline_plan_sha256": object_digest(plan), "diagnostics": diagnostics, "candidates": items})
        for p in (graph, old_summary, old_plan): inputs[str(p)] = digest(p)
    for p in source_files:
        target = out / "source_snapshot" / p.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    manifest = {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "fixed historical-baseline mechanism probe, not formal_v2 equal-budget comparison",
        "python": sys.version, "python_executable": str(Path(sys.executable).resolve()),
        "source_sha256": {str(p): digest(p) for p in source_files}, "input_sha256": inputs,
        "num_cores": 5, "seed": 17, "timeout_seconds": 60, "workers": 1,
        "max_p2_calls": 45, "max_p3_calls": 5, "planned_p2_calls": sum(len(x["candidates"]) for x in cases),
        "p3_rule": "one fixed selected P2 plan per case only when makespan strictly beats its historical baseline; no P3 search",
        "cases": cases}
    atomic_json(out / "manifest.json", manifest)
    return {"planned_p2_calls": manifest["planned_p2_calls"], "cases": [
        {"case": r["case"], "baseline": key(r["baseline_record"]), "candidates": len(r["candidates"])} for r in cases]}


def run(out):
    m = read(out / "manifest.json")
    if (out / "progress.json").exists() or (out / "evaluations").exists():
        raise ValueError("run already started; keep evidence and do not automatically retry")
    frozen = {**m["source_sha256"], **m["input_sha256"]}
    if any(digest(p) != h for p, h in frozen.items()):
        raise ValueError("pre-registered source/input hash changed")
    cases, calls = [], []
    for case in m["cases"]:
        evaluations = []
        for candidate in case["candidates"]:
            if digest(candidate["plan_path"]) != candidate["plan_sha256"]:
                raise ValueError("frozen candidate plan changed")
            record = evaluate(case["graph_path"], read(candidate["plan_path"]), 2,
                out / "evaluations", timeout=60, config_path=DATA / "config.txt")
            row = {**candidate, "record": record}
            evaluations.append(row)
            calls.append({"case": case["case"], "problem": 2, **row})
            atomic_json(out / case["case"] / f"candidate_{candidate['index']:02d}.record.json", row)
            atomic_json(out / "progress.json", {"completed_calls": len(calls), "calls": calls})
            print(json.dumps({"case": case["case"], "name": candidate["name"], "problem": 2,
                "status": record["status"], "metrics": record.get("metrics")}, ensure_ascii=False), flush=True)
        successes = [r for r in evaluations if r["record"]["status"] == "success"]
        best = min(successes, key=lambda r: key(r["record"])) if successes else None
        selected = best if best and key(best["record"]) < key(case["baseline_record"]) else None
        row = {"case": case["case"], "baseline_record": case["baseline_record"],
            "evaluations": evaluations, "best_new": best, "selected_new": selected,
            "selected_record": selected["record"] if selected else case["baseline_record"],
            "selected_plan_path": selected["plan_path"] if selected else case["baseline_plan_path"],
            "strict_time_improvement": bool(selected and key(selected["record"])[0] < key(case["baseline_record"])[0])}
        atomic_json(out / case["case"] / "summary.json", row)
        cases.append(row)
    for case in cases:
        if case["strict_time_improvement"]:
            source = next(x for x in m["cases"] if x["case"] == case["case"])
            plan = read(case["selected_plan_path"])
            record = evaluate(source["graph_path"], plan, 3, out / "evaluations", timeout=60,
                              config_path=DATA / "config.txt")
            case["fixed_plan_p3_record"] = record
            calls.append({"case": case["case"], "problem": 3, "plan_path": case["selected_plan_path"], "record": record})
            atomic_json(out / case["case"] / "fixed_plan_p3.record.json", record)
            atomic_json(out / case["case"] / "summary.json", case)
            atomic_json(out / "progress.json", {"completed_calls": len(calls), "calls": calls})
            print(json.dumps({"case": case["case"], "problem": 3, "status": record["status"],
                              "metrics": record.get("metrics")}, ensure_ascii=False), flush=True)
    summary = {"scope": m["scope"], "manifest_sha256": digest(out / "manifest.json"),
        "p2_calls": sum(r["problem"] == 2 for r in calls), "p3_calls": sum(r["problem"] == 3 for r in calls),
        "status_counts": dict(Counter(r["record"]["status"] for r in calls)),
        "cache_hits": sum(r["record"]["cache_hit"] for r in calls),
        "sources_and_inputs_unchanged": all(digest(p) == h for p, h in frozen.items()),
        "strict_time_improvement_cases": [c["case"] for c in cases if c["strict_time_improvement"]], "cases": cases}
    atomic_json(out / "summary.json", summary)
    return {k: v for k, v in summary.items() if k != "cases"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("prepare", "run"))
    ap.add_argument("run_dir", type=Path)
    args = ap.parse_args()
    destination = args.run_dir.resolve()
    if destination == DATA.parent or DATA.parent in destination.parents:
        raise ValueError("cannot write probe into official inputs")
    print(json.dumps(prepare(destination) if args.action == "prepare" else run(destination), ensure_ascii=False, indent=2))
