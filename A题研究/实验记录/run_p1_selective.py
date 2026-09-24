#!/usr/bin/env python3
"""Frozen, extra-budget P1 selective decomposition probe (not formal v2)."""
import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
sys.path[:0] = [str(RESEARCH), str(RESEARCH / "精修求解器")]
from p1_selective import generate_selective_candidates, task_lower_bound
from advanced_solver.engine import source_hashes
from solver.common import DATA, atomic_json, read_json, object_digest, digest
from solver.evaluator import evaluate
from solver.graph_ir import GraphIR
from solver.plan import validate_plan


def score(record):
    m = record["metrics"]
    return m["makespan"], m["data_movement_bytes"]["added_copy_bytes"]


def verify(record, graph, plan, config, cores):
    if record["status"] != "success":
        raise ValueError("successful evidence required")
    for key, expected in (("graph_sha256", digest(graph)), ("config_sha256", digest(config)),
                          ("plan_sha256", object_digest(plan))):
        if record["hashes"][key] != expected:
            raise ValueError("evidence hash mismatch: " + key)
    if digest(record["result_path"]) != record["result_sha256"]:
        raise ValueError("raw result changed")
    with gzip.open(record["result_path"], "rt", encoding="utf-8") as f:
        raw = json.load(f)
    if raw["scene"] != "A" or raw["num_cores"] != cores or raw["makespan"] != score(record)[0]:
        raise ValueError("wrong official scene, cores or objective")
    if raw["data_movement_bytes"]["added_copy_bytes"] != score(record)[1]:
        raise ValueError("raw COPY metric mismatch")
    return raw


def main(args):
    out = args.run_dir.resolve()
    if out.exists() or out == DATA.parent or DATA.parent in out.parents:
        raise ValueError("fresh output outside official attachments required")
    cache = args.evaluation_dir.resolve() if args.evaluation_dir else out / "evaluations"
    if cache == DATA.parent or DATA.parent in cache.parents:
        raise ValueError("evaluation outputs cannot overwrite attachments")
    cases = [f"case_{int(c):03d}" for c in args.cases.split(",")]
    if len(cases) != len(set(cases)):
        raise ValueError("duplicate experiment cases")
    config = DATA / "config.txt"
    inherited_path = RESEARCH / "实验记录/core_inheritance_v1/summary.json"
    inherited = read_json(inherited_path)["results"] if inherited_path.exists() else []
    prepared = []
    for case in cases:
        graph = DATA / (case + ".json")
        ir = GraphIR.from_path(graph)
        initial_path = RESEARCH / f"当前最佳方案_v2/p1/n{args.num_cores}/{case}_multicore_res.json"
        initial = read_json(initial_path)
        source = read_json(initial_path.with_suffix(".provenance.json"))["evaluation_record"]
        verify(source, graph, initial, config, args.num_cores)
        for other in inherited:
            if other["case"] == case and other["problem"] == 1 and other["num_cores"] == args.num_cores and other["accepted"] and score(other["record"]) < score(source):
                source, initial = other["record"], other["plan"]
                verify(source, graph, initial, config, args.num_cores)
        candidates, diag = generate_selective_candidates(ir, args.num_cores, args.candidate_cap, args.seed)
        prepared.append({"case": case, "graph_path": str(graph), "graph_sha256": digest(graph),
                         "initial_plan": initial, "initial_record": source,
                         "initial_plan_sha256": object_digest(initial),
                         "candidates": candidates, "diagnostics": diag,
                         "timeout": 60 if len(ir.compute_ids) <= 10000 else 180})
    sources = {**source_hashes(), str(Path(__file__).resolve()): digest(__file__),
               str(RESEARCH / "精修求解器/p1_selective.py"): digest(RESEARCH / "精修求解器/p1_selective.py")}
    manifest = {"experiment": "P1 selective WCC decomposition, extra-budget mechanism probe",
                "formal_v2_unchanged": True, "num_cores": args.num_cores, "seed": args.seed,
                "candidate_cap": args.candidate_cap, "logical_cap": len(cases) * (args.candidate_cap + 1),
                "config_path": str(config), "config_sha256": digest(config), "source_sha256": sources,
                "evaluation_dir": str(cache), "cases": prepared,
                "pruning": "Only fixed-plan certified lower bound strictly above current official makespan; equality is retained for secondary COPY objective.",
                "accounting": "Initial reevaluation, failures and exact-cache hits all charged. Duplicate plans and provably dominated plans do not call evaluator.",
                "selection": "Original official success, then lexicographic makespan and added COPY bytes"}
    atomic_json(out / "manifest.json", manifest)
    results, started = [], time.perf_counter()
    for entry in prepared:
        graph, case = Path(entry["graph_path"]), entry["case"]
        ir = GraphIR.from_path(graph)
        best, seen, calls, skipped = None, set(), [], []
        initial = {"name": "frozen_incumbent", "plan": entry["initial_plan"],
                   "metadata": {"plan_lower_bound": task_lower_bound(ir, entry["initial_plan"])}}
        for candidate in [initial] + entry["candidates"]:
            hashed = object_digest(candidate["plan"])
            bound = candidate["metadata"]["plan_lower_bound"]["value"]
            reason = "duplicate" if hashed in seen else "bound_dominated" if best and bound > score(best["record"])[0] else None
            if reason:
                skipped.append({"name": candidate["name"], "reason": reason, "bound": bound,
                                "incumbent_score": list(score(best["record"])) if best else None})
                continue
            seen.add(hashed)
            validate_plan(ir, candidate["plan"])
            row = {"index": len(calls), "name": candidate["name"], "plan_sha256": hashed,
                   "state": "pending", "bound": bound, "metadata": candidate["metadata"]}
            calls.append(row)
            atomic_json(out / case / "checkpoint.json", {"calls": calls, "skipped": skipped, "best": best})
            record = evaluate(graph, candidate["plan"], 1, cache, timeout=entry["timeout"], config_path=config)
            row.update({"state": "returned", "record": record, "accepted": False})
            if record["status"] == "success":
                verify(record, graph, candidate["plan"], config, args.num_cores)
                if bound > score(record)[0]:
                    raise AssertionError("claimed lower bound exceeds actual official makespan")
                if best is None or score(record) < score(best["record"]):
                    best = {**candidate, "record": record}
                    row["accepted"] = True
                    atomic_json(out / case / "selected.plan.json", candidate["plan"])
            atomic_json(out / case / "checkpoint.json", {"calls": calls, "skipped": skipped, "best": best})
        result = {"case": case, "initial_score": list(score(entry["initial_record"])),
                  "best": best, "calls": calls, "skipped": skipped,
                  "improved": best is not None and score(best["record"]) < score(entry["initial_record"])}
        results.append(result)
        atomic_json(out / case / "summary.json", result)
        atomic_json(out / "progress.json", {"completed": len(results), "expected": len(prepared), "results": results})
        print(json.dumps({"case": case, "initial": result["initial_score"],
                          "best": score(best["record"]) if best else None, "calls": len(calls),
                          "pruned": sum(x["reason"] == "bound_dominated" for x in skipped)}), flush=True)
    if not all(digest(p) == h for p, h in sources.items()):
        raise ValueError("source changed during experiment")
    if digest(config) != manifest["config_sha256"] or not all(digest(e["graph_path"]) == e["graph_sha256"] for e in prepared):
        raise ValueError("input changed during experiment")
    all_calls = [c for r in results for c in r["calls"]]
    summary = {"completed": True, "sources_and_inputs_unchanged": True, "results": results,
               "logical_calls": len(all_calls), "logical_cap": manifest["logical_cap"],
               "status_counts": dict(Counter(c["record"]["status"] for c in all_calls)),
               "cache_hits": sum(bool(c["record"].get("cache_hit")) for c in all_calls),
               "improved_cases": sum(r["improved"] for r in results), "elapsed_seconds": time.perf_counter() - started}
    atomic_json(out / "summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="002,003,005,008,009,016")
    parser.add_argument("-n", "--num-cores", type=int, choices=range(1, 6), default=5)
    parser.add_argument("--candidate-cap", type=int, default=12)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--evaluation-dir", type=Path)
    args = parser.parse_args()
    if args.candidate_cap < 1:
        parser.error("positive candidate cap required")
    main(args)
