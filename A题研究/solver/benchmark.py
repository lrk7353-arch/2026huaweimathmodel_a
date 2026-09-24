"""Checkpointed per-graph benchmark, with bounded independent evaluator processes."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import shutil
import sys
import time
import traceback

sys.dont_write_bytecode = True
from common import DATA, HERE, atomic_json, digest, read_json, source_manifest
from evaluator import evaluate
from graph_ir import GraphIR
from solve import solve_one


def case_paths(data, cases):
    if cases == "all":
        paths = sorted(data.glob("case_[0-9][0-9][0-9].json"))
        if len(paths) != 100:
            raise ValueError(f"Expected 100 official cases, found {len(paths)}")
        return paths
    names = []
    for item in cases.split(","):
        name = item.strip()
        if name.isdecimal():
            name = "case_" + name.zfill(3)
        path = data / (name + ".json")
        if not path.is_file():
            raise ValueError(f"Missing graph {path}")
        if path not in names:
            names.append(path)
    return names


def run_graph(path, args, manifest):
    results_dir = args.run_dir / "results" / path.stem
    results_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    ir = GraphIR.from_path(path)
    baseline = evaluate(path, None, 0, args.run_dir / "evaluations", timeout=args.timeout,
                        config_path=args.config)
    atomic_json(results_dir / "singlecore.json", baseline)
    failed_slots = 0
    failed_pairs = 0
    for method in args.methods:
        for problem in args.problems:
            inherited = []
            for cores in args.cores:
                target = results_dir / f"{method}_p{problem}_n{cores}_seed{args.seed}.json"
                result = None
                if target.exists():
                    old = read_json(target)
                    if (old.get("benchmark_signature") == manifest["signature"]
                            and old.get("status") == "success"):
                        # Re-evaluation cache verifies compressed output integrity on resume.
                        check = evaluate(path, old["best"]["plan"], problem,
                                         args.run_dir / "evaluations", timeout=args.timeout,
                                         config_path=args.config)
                        if check["status"] == "success" and check["metrics"]["makespan"] == old["best"]["record"]["metrics"]["makespan"]:
                            result = old
                if result is None:
                    incumbents = list(inherited)
                    if method == "affinity" and "simple" in args.methods:
                        control_path = results_dir / f"simple_p{problem}_n{cores}_seed{args.seed}.json"
                        if control_path.exists():
                            control = read_json(control_path)
                            if control.get("benchmark_signature") == manifest["signature"] and control.get("best"):
                                incumbents.insert(0, {"name": f"simple_n{cores}", "plan": control["best"]["plan"]})
                    result = solve_one(path, cores, problem, method, args.run_dir,
                                       timeout=args.timeout, seed=args.seed,
                                       max_evaluations=args.max_evaluations,
                                       budget_seconds=args.budget_seconds,
                                       config_path=args.config, ir=ir, inherited=incumbents)
                    result["benchmark_signature"] = manifest["signature"]
                    atomic_json(target, result)
                if result["best"]:
                    inherited.append({"name": f"n{cores}", "plan": result["best"]["plan"]})
                    atomic_json(target.with_suffix(".plan.json"), result["best"]["plan"])
                else:
                    failed_slots += 1
                print(json.dumps({"case": path.stem, "method": method, "p": problem, "n": cores,
                                  "status": result["status"],
                                  "makespan": result["best"]["record"]["metrics"]["makespan"] if result["best"] else None,
                                  "evaluations": result["evaluated_count"],
                                  "solve_seconds": round(result["elapsed_seconds"], 3)}, ensure_ascii=False), flush=True)
        if 2 in args.problems and 3 in args.problems:
            for cores in args.cores:
                summaries = [read_json(results_dir / f"{method}_p{problem}_n{cores}_seed{args.seed}.json")
                             for problem in (2, 3)]
                if not all(s["best"] for s in summaries):
                    failed_pairs += 1
                    continue
                pi2, pi3 = [s["best"]["plan"] for s in summaries]
                cells = {"t2_pi2": summaries[0]["best"]["record"],
                         "t3_pi3": summaries[1]["best"]["record"],
                         "t3_pi2": evaluate(path, pi2, 3, args.run_dir / "evaluations",
                                             timeout=args.timeout, config_path=args.config),
                         "t2_pi3": evaluate(path, pi3, 2, args.run_dir / "evaluations",
                                             timeout=args.timeout, config_path=args.config)}
                values = {k: v["metrics"]["makespan"] for k,v in cells.items() if v["status"] == "success"}
                ratios = {}
                if len(values) == 4:
                    ratios = {"hardware": values["t2_pi2"] / values["t3_pi2"],
                              "selection": values["t3_pi2"] / values["t3_pi3"],
                              "total": values["t2_pi2"] / values["t3_pi3"]}
                else:
                    failed_pairs += 1
                atomic_json(results_dir / f"{method}_cache_pair_n{cores}_seed{args.seed}.json",
                            {"case": path.stem, "method": method, "num_cores": cores,
                             "cells": cells, "ratios": ratios,
                             "scope": "same baseline candidate pool; no cache-specific reorder search"})
    return {"case": path.stem, "baseline_status": baseline["status"],
            "failed_slots": failed_slots, "failed_pairs": failed_pairs,
            "complete": baseline["status"] == "success" and failed_slots == 0 and failed_pairs == 0,
            "elapsed_seconds": time.monotonic()-started}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=DATA)
    p.add_argument("--config", type=Path)
    p.add_argument("--cases", default="001,093,071")
    p.add_argument("--cores", default="2,5")
    p.add_argument("--problems", default="1,2,3")
    p.add_argument("--methods", default="simple,affinity")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--max-evaluations", type=int)
    p.add_argument("--budget-seconds", type=float)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--run-dir", type=Path, required=True)
    args = p.parse_args(argv)
    args.data = args.data.resolve()
    args.run_dir = args.run_dir.resolve()
    args.config = (args.config or args.data / "config.txt").resolve()
    args.cores = sorted(set(map(int, args.cores.split(","))))
    args.problems = sorted(set(map(int, args.problems.split(","))))
    args.methods = sorted(set(args.methods.split(",")), key=lambda name: name != "simple")
    if (not args.cores or not set(args.cores) <= set(range(1,6)) or
        not set(args.problems) <= {1,2,3} or not set(args.methods) <= {"simple","affinity"} or args.workers < 1):
        p.error("invalid cores, problems, methods, or workers")
    if args.run_dir.is_relative_to(args.data):
        p.error("run directory must be outside original inputs")
    paths = case_paths(args.data, args.cases)
    from common import object_digest
    specification = {"inputs": {path.name: digest(path) for path in paths},
                     "config": digest(args.config), "sources": source_manifest(),
                     "cores": args.cores, "problems": args.problems, "methods": args.methods,
                     "seed": args.seed, "timeout": args.timeout, "workers": args.workers,
                     "max_evaluations": args.max_evaluations, "budget_seconds": args.budget_seconds,
                     "python": platform.python_version()}
    manifest = {"signature": object_digest(specification), "specification": specification,
                "started_utc": datetime.now(timezone.utc).isoformat(),
                "command": sys.argv, "platform": platform.platform(),
                "scope": "pilot" if len(paths) < 100 else "all_official_cases"}
    target_manifest = args.run_dir / "manifest.json"
    if target_manifest.exists() and read_json(target_manifest).get("signature") != manifest["signature"]:
        p.error("run-dir already belongs to different code/settings; choose a new directory")
    atomic_json(target_manifest, manifest)
    for relative in specification["sources"]:
        from common import ROOT
        destination = args.run_dir / "source_snapshot" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    completed, failures = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(run_graph, path, args, manifest): path for path in paths}
        for job in as_completed(jobs):
            try:
                completed.append(job.result())
            except Exception:
                failure = {"case": jobs[job].stem, "error": traceback.format_exc()}
                failures.append(failure)
                print(json.dumps(failure, ensure_ascii=False), flush=True)
            atomic_json(args.run_dir / "progress.json", {"completed": completed, "failures": failures,
                                                       "expected_cases": len(paths)})
    incomplete = sum(not r["complete"] for r in completed)
    print(json.dumps({"completed_cases": len(completed), "failed_cases": len(failures), "incomplete_cases": incomplete,
                      "run_dir": str(args.run_dir)}, ensure_ascii=False))
    return 1 if failures or incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
