#!/usr/bin/env python3
"""Hard whole-process search deadline, including generation and official children.

POSIX process groups ensure a timed-out evaluator cannot keep running. Only a
successful hash-verified official checkpoint may be recovered after termination.
"""
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from advanced_solver.solve import parser, output_path
from advanced_solver.engine import atomic_json, read_json, load_official, digest, object_digest, source_hashes


def supervise(command, seconds, log_path):
    """Return process outcome, killing the whole child group on the hard limit."""
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("wall budget must be finite and positive")
    start = time.perf_counter()
    timed_out = False
    with open(log_path, "wb") as log:
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        try:
            code = child.wait(timeout=max(.001, seconds - (time.perf_counter() - start)))
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            code = child.wait()
        except BaseException:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            raise
    return {"timed_out": timed_out, "child_returncode": code,
            "wall_seconds": time.perf_counter() - start, "hard_budget_seconds": seconds,
            "scope": "child interpreter, graph parsing, candidate generation, evaluation and checkpoint IO"}


def recover(run_dir, outcome):
    summary_path = run_dir / "summary.json"
    checkpoint = summary_path if summary_path.exists() else run_dir / "checkpoint.json"
    if checkpoint.exists():
        result = read_json(checkpoint)
        if source_hashes() != result["source_sha256"]:
            raise ValueError("source changed; cannot certify recovered result")
        if digest(result["graph_path"]) != result["graph_sha256"] or digest(result["config_path"]) != result["config_sha256"]:
            raise ValueError("input changed; cannot certify recovered result")
        if result.get("best"):
            row = result["best"]
            load_official(row["record"])
            if object_digest(row["plan"]) != row["record"]["hashes"]["plan_sha256"]:
                raise ValueError("checkpoint plan hash does not match official success")
        if outcome["timed_out"] or outcome["child_returncode"] != 0:
            result.update({"state": "supervisor_timeout" if outcome["timed_out"] else "child_failed",
                           "completed": False, "stop_reason": "hard_wall_budget" if outcome["timed_out"] else "child_failure"})
    else:
        result = {"status": "no_feasible_result", "best": None, "completed": False,
                  "state": "supervisor_timeout" if outcome["timed_out"] else "child_failed",
                  "evaluations": [], "evaluated_count": 0}
    result["supervisor"] = outcome
    atomic_json(summary_path, result)
    if result.get("best"):
        atomic_json(run_dir / "best.plan.json", result["best"]["plan"])
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = parser()
    p.add_argument("--wall-budget", type=float, required=True)
    args = p.parse_args(argv)
    output = output_path(args)
    run_dir = args.run_dir.resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError("hard-budget run directory must be fresh")
    if not math.isfinite(args.wall_budget) or args.wall_budget <= 0:
        raise ValueError("wall budget must be finite and positive")
    # Keep the child run directory empty; its log is a sibling until termination.
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    log_path = run_dir.parent / (run_dir.name + ".child.log")
    if log_path.exists():
        raise ValueError("existing child log: choose a fresh attempt directory")
    child_args = []
    index = 0
    while index < len(argv):
        if argv[index] == "--wall-budget":
            index += 2
        elif argv[index].startswith("--wall-budget="):
            index += 1
        else:
            child_args.append(argv[index])
            index += 1
    outcome = supervise([sys.executable, "-B", str(Path(__file__).with_name("solve.py")), *child_args], args.wall_budget, log_path)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path.replace(run_dir / "child.log")
    atomic_json(run_dir / "supervisor.json", outcome)
    result = recover(run_dir, outcome)
    if result.get("best"):
        atomic_json(output, result["best"]["plan"])
    print(json.dumps({"status": result["status"], "completed": result["completed"], **outcome}, ensure_ascii=False))
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(2)
