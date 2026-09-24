#!/usr/bin/env python3
"""Experimental unified P2/P3 entry; not yet a full-dataset/equal-budget baseline.

Only unmodified official successful evaluations can update the incumbent.
Candidate families and their order are fixed before evaluation; no case-ID rules.
"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sys
import time
import uuid

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SOLVER = HERE.parent / "solver"
sys.path.insert(0, str(SOLVER))
from graph_ir import GraphIR
from plan import validate_plan
from baselines import generate_candidates as component_candidates
from evaluator import evaluate
from common import DATA, OFFICIAL, atomic_json, digest, object_digest, read_json, single_active_plan
from operation_heft_probe import dependency_views, operation_assignment, runs_plan
from partition_candidates import topological_order, contiguous_blocks, block_views, assign_blocks


FIXED_SETTINGS = {
    ("capacity", "L1"): 524288, ("capacity", "UB"): 131072,
    ("bandwidth", "bandwidth"): 60,
    ("multicore_scene_a", "task_cross_core_wait_cycles"): 1000,
    ("multicore_scene_a", "task_same_core_wait_cycles"): 100,
    ("multicore_scene_b", "cross_core_copy_delay_cycles"): 500,
    ("problem_3", "cache_capacity_bytes"): 1048576,
    ("problem_3", "cache_bandwidth_bytes_per_cycle"): 250,
}


def check_fixed_config(path):
    """Allow an unchanged-settings copy/comment edit, never a different machine."""
    settings, section = {}, None
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section not in {s for s, _ in FIXED_SETTINGS}:
                raise ValueError("unknown configuration section at line {}".format(number))
            continue
        pieces = line.split()
        if len(pieces) != 2 or section is None:
            raise ValueError("malformed configuration line {}".format(number))
        key = section, pieces[0]
        if key in settings:
            raise ValueError("duplicate configuration setting")
        settings[key] = int(pieces[1])
    if settings != FIXED_SETTINGS:
        raise ValueError("configuration must keep every fixed official setting unchanged")
    return settings


def _outside_official(path):
    path = Path(path).resolve()
    try:
        path.relative_to(DATA.parent.resolve())
    except ValueError:
        return path
    raise ValueError("generated artifacts must be outside the official attachment directory")


def checked_output_paths(output_path, graph_path, config_path, incumbent_path=None):
    """Resolve and reject artifact/input aliases before solving or writing.

    Keep the existing suffix convention for compatibility. Resolve the derived
    report separately to catch report symlinks aliasing an input or output.
    """
    output = _outside_official(output_path)
    summary = _outside_official(output.with_suffix(".advanced.json"))
    if output == summary:
        raise ValueError("plan output and summary resolve to the same path; choose another output path")
    inputs = {"graph": Path(graph_path).resolve(), "config": Path(config_path).resolve()}
    if incumbent_path is not None:
        inputs["incumbent"] = Path(incumbent_path).resolve()
    for role, artifact in (("plan output", output), ("summary", summary)):
        for source, path in inputs.items():
            if artifact == path:
                raise ValueError("{} must not overwrite {} input: {}".format(role, source, path))
    return output, summary


def _checked_incumbent(ir, plan, cores):
    validate_plan(ir, plan)
    if len(plan["core_schedules"]) > cores:
        raise ValueError("incumbent has more hardware core lists than requested")
    padded = {"node_to_subgraph": dict(plan["node_to_subgraph"]),
              "core_schedules": [list(s) for s in plan["core_schedules"]]
              + [[] for _ in range(cores - len(plan["core_schedules"]))]}
    validate_plan(ir, padded)
    return padded


def generate_advanced_candidates(ir, num_cores, incumbent_plan=None):
    """Return (candidate records, generation failures), without evaluator calls.

    Exact submitted JSON order is part of the dedup key, as in the existing
    wrapper. No unproved equivalence of different object orders is assumed.
    """
    if type(num_cores) is not int or not 1 <= num_cores <= 5:
        raise ValueError("num_cores must be 1..5")
    records, seen, failures = [], {}, []

    def add(name, plan, metadata):
        validate_plan(ir, plan)
        if len(plan["core_schedules"]) != num_cores:
            raise ValueError("candidate core count mismatch")
        signature = object_digest(plan)
        if signature in seen:
            seen[signature]["metadata"]["also_generated_as"].append({"name": name, **metadata})
            return
        metadata = {**metadata, "active_cores": sum(bool(s) for s in plan["core_schedules"]),
                    "subgraphs": len(set(plan["node_to_subgraph"].values())), "also_generated_as": []}
        record = {"name": name, "plan": plan, "metadata": metadata}
        records.append(record)
        seen[signature] = record

    # Prioritized so even a one-evaluation budget revalidates a supplied incumbent.
    if incumbent_plan is not None:
        add("provided_incumbent", _checked_incumbent(ir, incumbent_plan, num_cores),
            {"family": "incumbent", "requires_fresh_official_evaluation": True})
    add("safe_single_active", single_active_plan(ir.graph, num_cores), {"family": "safe"})
    target_active = min(num_cores, len(ir.components))
    try:
        for candidate in component_candidates(ir, num_cores, method="simple", seed=0):
            if candidate["metadata"]["active_cores"] == target_active:
                add("simple_target_" + candidate["metadata"]["granularity"], candidate["plan"],
                    {"family": "simple", "original_metadata": candidate["metadata"]})
    except Exception as error:
        failures.append({"family": "simple", "error": "{}: {}".format(type(error).__name__, error)})
    if not ir.compute_ids:
        return records, failures
    orders = {}
    for ordering in ("critical_path", "stable_id"):
        try:
            orders[ordering] = topological_order(ir, ordering)
        except Exception as error:
            failures.append({"family": "topology", "ordering": ordering, "error": str(error)})
    try:
        views = dependency_views(ir)
    except Exception as error:
        views = None
        failures.append({"family": "heft_views", "error": "{}: {}".format(type(error).__name__, error)})
    # Full communication penalty first; both orderings before optimistic weight.
    for weight in (1.0, 0.25):
        for ordering, order in orders.items():
            if views is None:
                continue
            name = "heft_{}_w{:03d}".format(ordering, int(weight * 100))
            try:
                assignment, proxy = operation_assignment(ir, order, weight, views, num_cores=num_cores)
                add(name, runs_plan(ir, order, assignment, num_cores=num_cores),
                    {"family": "heft", "ordering": ordering, "communication_proxy_weight": weight,
                     "official_delay": 500, "official_bandwidth": 60, **proxy})
            except Exception as error:
                failures.append({"family": "heft", "candidate": name,
                                 "error": "{}: {}".format(type(error).__name__, error)})
    priority_blocks = max(5, 4 * num_cores)
    for ordering, order in orders.items():
        try:
            blocks = contiguous_blocks(ir, order, priority_blocks)
            plan = {"node_to_subgraph": {str(op): bid for bid, block in enumerate(blocks) for op in block},
                    "core_schedules": [list(range(len(blocks)))] + [[] for _ in range(num_cores - 1)]}
            add("same_core_{}_b{}".format(ordering, len(blocks)), plan,
                {"family": "same_core_priority", "ordering": ordering,
                 "target_blocks": priority_blocks, "all_compute_core": 0})
        except Exception as error:
            failures.append({"family": "same_core_priority", "ordering": ordering, "error": str(error)})
    if "critical_path" in orders:
        try:
            blocks = contiguous_blocks(ir, orders["critical_path"], max(2, 2 * num_cores))
            view = block_views(ir, blocks)
            assignment, proxy = assign_blocks(ir, blocks, view, "communication_eft", num_cores=num_cores)
            schedules = [[] for _ in range(num_cores)]
            for bid, core in enumerate(assignment):
                schedules[core].append(bid)
            plan = {"node_to_subgraph": {str(op): bid for bid, block in enumerate(blocks) for op in block},
                    "core_schedules": schedules}
            add("contiguous_critical_b{}".format(len(blocks)), plan,
                {"family": "contiguous", "ordering": "critical_path",
                 "assignment_proxy": "P1-style EFT 100/1000, not a P2/P3 timing model", **proxy})
        except Exception as error:
            failures.append({"family": "contiguous", "error": "{}: {}".format(type(error).__name__, error)})
    return records, failures


def source_hashes():
    sources = [Path(__file__).resolve(), HERE / "operation_heft_probe.py", HERE / "partition_candidates.py"]
    sources += list(SOLVER.glob("*.py")) + list(OFFICIAL.glob("*.py"))
    return {str(p.resolve()): digest(p) for p in sorted(set(sources))}


def result_key(record):
    metrics = record["metrics"]
    return metrics["makespan"], metrics.get("data_movement_bytes", {}).get("added_copy_bytes", 0)


def solve_advanced(graph_path, num_cores, problem, run_dir, *, config_path=None,
                   timeout=45.0, max_evaluations=10, incumbent_plan=None):
    if type(problem) is not int or problem not in (2, 3):
        raise ValueError("experimental advanced entry supports P2/P3 only")
    if type(num_cores) is not int or not 1 <= num_cores <= 5:
        raise ValueError("num_cores must be 1..5")
    if type(max_evaluations) is not int or max_evaluations < 1:
        raise ValueError("max_evaluations must be a positive integer")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    graph_path = Path(graph_path).resolve()
    config_path = Path(config_path or graph_path.parent / "config.txt").resolve()
    check_fixed_config(config_path)
    run_dir = _outside_official(run_dir)
    session = run_dir / "solves" / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex)
    session.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    ir = GraphIR.from_path(graph_path)
    gen_started = time.perf_counter()
    candidates, generation_failures = generate_advanced_candidates(ir, num_cores, incumbent_plan)
    gen_seconds = time.perf_counter() - gen_started
    manifest = {"scope": "experimental unified entry; not yet full-dataset/equal-budget validated",
                "graph_path": str(graph_path), "graph_sha256": digest(graph_path),
                "config_path": str(config_path), "config_sha256": digest(config_path),
                "problem": problem, "num_cores": num_cores,
                "source_sha256": source_hashes(), "candidate_generation_seconds": gen_seconds,
                "budgets": {"max_evaluations": max_evaluations, "per_evaluation_timeout_seconds": timeout,
                            "worker_concurrency": 1, "total_wallclock_budget_seconds": None},
                "selection": "successful official makespan first; extra-copy bytes break ties",
                "deduplication": "exact submitted JSON order hash", "generation_failures": generation_failures,
                "candidates": [{"name": c["name"], "metadata": c["metadata"], "plan_hash": object_digest(c["plan"])}
                               for c in candidates]}
    atomic_json(session / "manifest.json", manifest)
    for candidate in candidates:
        atomic_json(session / "candidate_plans" / (candidate["name"] + ".json"), candidate["plan"])
    evaluations, best = [], None
    for candidate in candidates[:max_evaluations]:
        record = evaluate(graph_path, candidate["plan"], problem, run_dir / "evaluations",
                          timeout=timeout, config_path=config_path)
        row = {"candidate": candidate["name"], "metadata": candidate["metadata"],
               "plan_hash": object_digest(candidate["plan"]), "record": record}
        evaluations.append(row)
        if record["status"] == "success" and (best is None or result_key(record) < result_key(best["record"])):
            best = {**row, "plan": candidate["plan"]}
            atomic_json(session / "incumbent.plan.json", best["plan"])
        atomic_json(session / "evaluations.json", evaluations)
    omitted = [c["name"] for c in candidates[max_evaluations:]]
    summary = {**manifest, "session_dir": str(session), "case": graph_path.stem,
               "status": "success" if best is not None else "no_feasible_result", "best": best,
               "evaluations": evaluations, "generated_count": len(candidates), "evaluated_count": len(evaluations),
               "status_counts": dict(Counter(r["record"]["status"] for r in evaluations)),
               "official_calls": sum(not r["record"].get("cache_hit", False) for r in evaluations),
               "cache_hits": sum(bool(r["record"].get("cache_hit", False)) for r in evaluations),
               "not_evaluated_budget": omitted,
               "stop_reason": "candidate_budget" if omitted else "all_candidates_evaluated",
               "elapsed_seconds": time.perf_counter() - started}
    atomic_json(session / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph", type=Path)
    parser.add_argument("-n", "--num-cores", type=int, choices=range(1, 6), required=True)
    parser.add_argument("-p", "--problem", type=int, choices=(2, 3), required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--run-dir", type=Path, default=HERE / "runs" / "advanced_interactive")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--max-evaluations", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--incumbent-plan", type=Path)
    args = parser.parse_args(argv)
    try:
        config = Path(args.config or args.graph.parent / "config.txt").resolve()
        output, summary_path = checked_output_paths(args.output, args.graph, config, args.incumbent_plan)
        incumbent = read_json(args.incumbent_plan) if args.incumbent_plan else None
        result = solve_advanced(args.graph, args.num_cores, args.problem, args.run_dir, config_path=config,
                                timeout=args.timeout, max_evaluations=args.max_evaluations, incumbent_plan=incumbent)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    atomic_json(summary_path, result)
    if result["best"] is None:
        print(json.dumps({"status": result["status"], "summary": str(summary_path)}))
        return 1
    atomic_json(output, result["best"]["plan"])
    print(json.dumps({"status": "success", "makespan": result["best"]["record"]["metrics"]["makespan"],
                      "candidate": result["best"]["candidate"], "evaluated_count": result["evaluated_count"],
                      "stop_reason": result["stop_reason"], "output": str(output),
                      "summary": str(summary_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
