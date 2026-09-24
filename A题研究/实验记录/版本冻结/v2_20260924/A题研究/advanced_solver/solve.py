#!/usr/bin/env python3
"""Single-instance entry for the staged, officially verified solver."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from advanced_solver.engine import Search, DATA, OFFICIAL, atomic_json, read_json


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("graph", type=Path)
    p.add_argument("-n", "--num-cores", type=int, choices=range(1, 6), required=True)
    p.add_argument("-p", "--problem", type=int, choices=(1, 2, 3), required=True)
    p.add_argument("--config", type=Path)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("-o", "--output", type=Path)
    p.add_argument("--profile", choices=("component", "operation", "trace", "cache", "full"), default="full")
    p.add_argument("--seed", type=int, default=17)
    p.add_argument("--component-cap", type=int, default=6)
    p.add_argument("--operation-cap", type=int, default=12)
    p.add_argument("--trace-cap", type=int, default=24)
    p.add_argument("--cache-cap", type=int, default=24)
    p.add_argument("--max-rounds", type=int, default=3)
    p.add_argument("--max-evaluations", type=int)
    p.add_argument("--timeout", type=float)
    p.add_argument("--total-budget-seconds", type=float, help="Cooperative deadline; use supervisor for a hard deadline")
    p.add_argument("--incumbent-plan", type=Path)
    p.add_argument("--evaluation-dir", type=Path, help="Exact-plan cache; logical trial cap still charges cache hits")
    return p


def output_path(args):
    run = args.run_dir.resolve()
    output = (args.output or run / "best.plan.json").resolve()
    config = (args.config or args.graph.parent / "config.txt").resolve()
    inputs = {args.graph.resolve(), config}
    if args.incumbent_plan:
        inputs.add(args.incumbent_plan.resolve())
    inputs.update(p.resolve() for p in OFFICIAL.glob("*.py"))
    inputs.update(p.resolve() for p in Path(__file__).parent.glob("*.py"))
    if output in inputs or output == DATA.parent.resolve() or DATA.parent.resolve() in output.parents:
        raise ValueError("output aliases an input/source or the official attachment directory")
    # Reject replacing any non-plan audit artifact, including symbolic aliases.
    reserved = {"manifest.json", "checkpoint.json", "summary.json", "supervisor.json", "child.log"}
    if output in {run / name for name in reserved} or output.exists():
        raise ValueError("output must be a fresh plan path and cannot replace evidence")
    if output.suffix != ".json":
        raise ValueError("plan output must be a .json file")
    return output


def execute(args):
    output = output_path(args)
    incumbent = read_json(args.incumbent_plan) if args.incumbent_plan else None
    search = Search(args.graph, args.num_cores, args.problem, args.run_dir,
                    config_path=args.config, profile=args.profile, seed=args.seed,
                    budgets={"component": args.component_cap, "operation": args.operation_cap,
                             "trace": args.trace_cap, "cache": args.cache_cap},
                    max_rounds=args.max_rounds, timeout=args.timeout,
                    total_budget_seconds=args.total_budget_seconds,
                    total_evaluations=args.max_evaluations, incumbent_plan=incumbent,
                    evaluation_dir=args.evaluation_dir)
    result = search.run()
    if result["best"]:
        atomic_json(output, result["best"]["plan"])
    print(json.dumps({"status": result["status"], "case": result["case"], "problem": result["problem"],
                      "num_cores": result["num_cores"], "evaluated_count": result["evaluated_count"],
                      "makespan": result["best"]["record"]["metrics"]["makespan"] if result["best"] else None,
                      "output": str(output), "summary": str(search.run_dir / "summary.json")}, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(execute(parser().parse_args()))
    except (ValueError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(2)
