"""Generate feasible candidates and select ONLY by unmodified official results."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
from common import (DATA, HERE, active_cores, atomic_json, digest, object_digest,
                    read_json, single_active_plan, source_manifest)
from graph_ir import GraphIR
from baselines import generate_candidates
from evaluator import evaluate


def result_key(record):
    metrics = record["metrics"]
    movement = metrics.get("data_movement_bytes", {})
    return (metrics["makespan"], movement.get("added_copy_bytes", 0))


def solve_one(graph_path, num_cores, problem, method, run_dir, *, timeout=120.0,
              seed=0, max_evaluations=None, budget_seconds=None, config_path=None,
              ir=None, inherited=None):
    if problem not in (1, 2, 3) or not 1 <= num_cores <= 5:
        raise ValueError("problem must be 1..3 and cores 1..5")
    if method not in ("simple", "affinity"):
        raise ValueError("method must be simple or affinity")
    if max_evaluations is not None and max_evaluations < 1:
        raise ValueError("max_evaluations must be positive")
    if timeout <= 0 or (budget_seconds is not None and budget_seconds <= 0):
        raise ValueError("timeouts/budgets must be positive")
    graph_path, run_dir = Path(graph_path).resolve(), Path(run_dir).resolve()
    config_path = Path(config_path or graph_path.parent / "config.txt").resolve()
    started = time.monotonic()
    if ir is None:
        ir = GraphIR.from_path(graph_path)
    generation_start = time.monotonic()
    candidates = [{"name": "single_active_safe", "plan": single_active_plan(ir.graph, num_cores),
                   "metadata": {"source": "safe_incumbent"}}]
    for old in inherited or []:
        plan = old["plan"]
        if len(plan["core_schedules"]) > num_cores:
            continue
        padded = {"node_to_subgraph": dict(plan["node_to_subgraph"]),
                  "core_schedules": [list(x) for x in plan["core_schedules"]]
                  + [[] for _ in range(num_cores-len(plan["core_schedules"]))]}
        candidates.append({"name": "inherited_" + old["name"], "plan": padded,
                           "metadata": {"source": "verified_incumbent"}})
    generated = generate_candidates(ir, num_cores, method=method, seed=seed)
    generated.sort(key=lambda x: -active_cores(x["plan"]))
    candidates.extend(generated)
    unique, seen = [], set()
    for candidate in candidates:
        signature = object_digest(candidate["plan"])
        if signature not in seen:
            unique.append(candidate)
            seen.add(signature)
    generation_seconds = time.monotonic() - generation_start
    evaluations, best = [], None
    stop_reason = "all_candidates_evaluated"
    for candidate in unique:
        if max_evaluations is not None and len(evaluations) >= max_evaluations:
            stop_reason = "candidate_limit"
            break
        remaining = None if budget_seconds is None else budget_seconds - (time.monotonic()-started)
        if remaining is not None and remaining <= 0:
            stop_reason = "wallclock_budget"
            break
        attempt_timeout = timeout if remaining is None else min(timeout, remaining)
        record = evaluate(graph_path, candidate["plan"], problem, run_dir / "evaluations",
                          timeout=attempt_timeout, config_path=config_path)
        row = {"candidate": candidate["name"], "metadata": candidate["metadata"],
               "plan_hash": object_digest(candidate["plan"]), "record": record}
        evaluations.append(row)
        if record["status"] == "success" and (best is None or result_key(record) < result_key(best["record"])):
            best = {**row, "plan": candidate["plan"]}
    successful = sum(e["record"]["status"] == "success" for e in evaluations)
    summary = {
        "schema_version": 1, "case": graph_path.stem, "graph_path": str(graph_path),
        "problem": problem, "num_cores": num_cores, "method": method, "seed": seed,
        "status": "success" if best else "no_feasible_result",
        "generation_seconds": generation_seconds, "elapsed_seconds": time.monotonic()-started,
        "candidate_count": len(unique), "evaluated_count": len(evaluations),
        "successful_count": successful,
        "official_calls": sum(not e["record"].get("cache_hit", False) for e in evaluations),
        "cache_hits": sum(bool(e["record"].get("cache_hit", False)) for e in evaluations),
        "status_counts": dict(Counter(e["record"]["status"] for e in evaluations)),
        "stop_reason": stop_reason,
        "budgets": {"per_evaluation_timeout": timeout, "max_evaluations": max_evaluations,
                    "wallclock_seconds": budget_seconds,
                    "wallclock_semantics": "soft end-to-end deadline checked before each evaluation; candidate generation is bounded by fixed loops, not preempted"},
        "input_hashes": {"graph": digest(graph_path), "config": digest(config_path)},
        "source_manifest": source_manifest(), "best": best, "evaluations": evaluations,
        "scope": "whole-dependency-component packing; no internal component splitting",
    }
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph", type=Path)
    parser.add_argument("-n", "--num-cores", type=int, required=True)
    parser.add_argument("-p", "--problem", type=int, required=True)
    parser.add_argument("--method", choices=("simple", "affinity"), default="affinity")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--run-dir", type=Path, default=HERE / "runs" / "interactive")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-evaluations", type=int)
    parser.add_argument("--budget-seconds", type=float)
    parser.add_argument("-o", "--output", type=Path, required=True,
                        help="Official two-field plan JSON; must not overwrite an original input")
    args = parser.parse_args(argv)
    if args.output.resolve() == args.graph.resolve() or args.output.resolve().is_relative_to(DATA.resolve()):
        parser.error("output must be outside the original data directory")
    result = solve_one(args.graph, args.num_cores, args.problem, args.method, args.run_dir,
                       timeout=args.timeout, seed=args.seed, max_evaluations=args.max_evaluations,
                       budget_seconds=args.budget_seconds, config_path=args.config)
    atomic_json(args.output.with_suffix(".solve.json"), result)
    if result["best"] is None:
        print(json.dumps({"status": result["status"], "counts": result["status_counts"]}))
        return 1
    atomic_json(args.output, result["best"]["plan"])
    print(json.dumps({"status": "success", "makespan": result["best"]["record"]["metrics"]["makespan"],
                      "candidate": result["best"]["candidate"], "plan": str(args.output),
                      "evaluations": result["evaluated_count"], "elapsed_seconds": result["elapsed_seconds"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
