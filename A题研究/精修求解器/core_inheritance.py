#!/usr/bin/env python3
"""Re-evaluate better lower-core plans after padding unused hardware cores.

This is an explicitly budgeted portfolio pass, separate from frozen algorithm
comparisons. No score is inferred from padding: every exported candidate must
have a successful original official evaluation at the requested target N.
"""
import argparse
import copy
import csv
import gzip
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
sys.path.insert(0, str(RESEARCH / "solver"))
from common import DATA, OFFICIAL, atomic_json, read_json, digest, object_digest
from evaluator import evaluate
from graph_ir import GraphIR
from plan import validate_plan


def pad_plan(plan, cores):
    if type(cores) is not int or not 1 <= cores <= 5:
        raise ValueError("target core count must be 1..5")
    if set(plan) != {"node_to_subgraph", "core_schedules"}:
        raise ValueError("plan must have the two official fields")
    if not 1 <= len(plan["core_schedules"]) <= cores:
        raise ValueError("cannot shrink or remove an existing core")
    result = copy.deepcopy(plan)
    result["core_schedules"].extend([[] for _ in range(cores - len(result["core_schedules"]))])
    return result


def run(catalog, out, evaluation_dir=None, max_evaluations=1200):
    catalog, out = Path(catalog).resolve(), Path(out).resolve()
    if out == DATA.parent or DATA.parent in out.parents:
        raise ValueError("artifacts cannot overwrite official inputs")
    if out.exists() and any(out.iterdir()):
        raise ValueError("use a fresh output directory")
    with catalog.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    available = {(r["case"], int(r["problem"]), int(r["num_cores"])): r for r in rows}
    proposals = []
    for (case, problem, target_n), target in sorted(available.items()):
        lowers = [available[case, problem, k] for k in range(1, target_n) if (case, problem, k) in available]
        if not lowers:
            continue
        source = min(lowers, key=lambda r: (int(r["makespan"]), int(r["added_copy_bytes"]), int(r["num_cores"])))
        if (int(source["makespan"]), int(source["added_copy_bytes"])) >= (int(target["makespan"]), int(target["added_copy_bytes"])):
            continue
        plan_path = Path(source["plan_path"])
        provenance = read_json(plan_path.with_suffix(".provenance.json"))
        record = provenance["evaluation_record"]
        plan = read_json(plan_path)
        if digest(plan_path) != source["plan_sha256"] or object_digest(plan) != record["hashes"]["plan_sha256"]:
            raise ValueError("source plan hash mismatch")
        if digest(record["result_path"]) != record["result_sha256"]:
            raise ValueError("source official output hash mismatch")
        with gzip.open(record["result_path"], "rt", encoding="utf-8") as f:
            raw = json.load(f)
        if raw["makespan"] != int(source["makespan"]):
            raise ValueError("catalog score differs from original official output")
        candidate = pad_plan(plan, target_n)
        proposals.append({"case": case, "problem": problem, "num_cores": target_n,
                          "source_num_cores": int(source["num_cores"]), "source_makespan": raw["makespan"],
                          "target_old_makespan": int(target["makespan"]),
                          "target_old_added_bytes": int(target["added_copy_bytes"]),
                          "source_record": record, "plan": candidate, "plan_sha256": object_digest(candidate)})
    proposals = proposals[:max_evaluations]
    out.mkdir(parents=True, exist_ok=True)
    sources = [Path(__file__)] + list((RESEARCH / "solver").glob("*.py")) + list(OFFICIAL.glob("*.py"))
    manifest = {"scope": "extra-budget best-known portfolio inheritance, not frozen equal-budget algorithm",
                "catalog_path": str(catalog), "catalog_sha256": digest(catalog),
                "source_sha256": {str(p): digest(p) for p in sources},
                "config_sha256": digest(DATA / "config.txt"), "logical_cap": max_evaluations,
                "selection": "one lowest-time then lowest-added-byte lower-N source per target, frozen before calls",
                "proposals": proposals}
    atomic_json(out / "manifest.json", manifest)
    evaluations = Path(evaluation_dir).resolve() if evaluation_dir else out / "evaluations"
    if evaluations == DATA.parent or DATA.parent in evaluations.parents:
        raise ValueError("evaluation outputs cannot overwrite official inputs")
    results = []
    for proposal in proposals:
        graph = DATA / (proposal["case"] + ".json")
        ir = GraphIR.from_path(graph)
        validate_plan(ir, proposal["plan"])
        directory = out / proposal["case"] / f"p{proposal['problem']}_n{proposal['num_cores']}"
        atomic_json(directory / "pending.json", proposal)
        record = evaluate(graph, proposal["plan"], proposal["problem"], evaluations,
                          timeout=60 if len(ir.compute_ids) <= 10000 else 180,
                          config_path=DATA / "config.txt")
        accepted = (record["status"] == "success" and
                    (record["metrics"]["makespan"], record["metrics"]["data_movement_bytes"]["added_copy_bytes"])
                    < (proposal["target_old_makespan"], proposal["target_old_added_bytes"]))
        result = {**proposal, "record": record, "accepted": accepted,
                  "padding_time_invariance_observed": record["status"] == "success" and record["metrics"]["makespan"] == proposal["source_makespan"]}
        atomic_json(directory / "result.json", result)
        if accepted:
            atomic_json(directory / "selected.plan.json", proposal["plan"])
        results.append(result)
        atomic_json(out / "progress.json", {"expected": len(proposals), "completed": len(results), "results": results})
    result = {"scope": manifest["scope"], "logical_calls": len(results), "accepted": sum(r["accepted"] for r in results),
              "all_padding_times_equal": all(r["padding_time_invariance_observed"] for r in results),
              "results": results, "sources_unchanged": all(digest(p) == h for p, h in manifest["source_sha256"].items())}
    atomic_json(out / "summary.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--evaluation-dir", type=Path)
    parser.add_argument("--max-evaluations", type=int, default=1200)
    args = parser.parse_args()
    if args.max_evaluations < 1:
        parser.error("evaluation cap must be positive")
    value = run(args.catalog, args.run_dir, args.evaluation_dir, args.max_evaluations)
    print(json.dumps({k: value[k] for k in ("logical_calls", "accepted", "all_padding_times_equal", "sources_unchanged")}))
