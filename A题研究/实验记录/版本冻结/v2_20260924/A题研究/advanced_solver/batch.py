#!/usr/bin/env python3
"""Auditable batch orchestration: sequential slots per graph, parallel graphs.

Never imports or executes an evaluator directly. Each fresh attempt invokes
solve.py or supervisor.py in a child process. Resume verifies immutable inputs,
settings, source hashes and all successful raw official-result/plan hashes.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
sys.path.insert(0, str(RESEARCH / "solver"))
from common import DATA, OFFICIAL, atomic_json, digest, object_digest, read_json


def source_hashes():
    # Match engine.source_hashes exactly; unrelated orchestration development
    # must not invalidate a running single-instance search.
    paths = [HERE / name for name in ("__init__.py", "engine.py", "solve.py", "supervisor.py",
             "operation_assign.py", "component_baseline.py", "trace_refine.py", "cache_refine.py")]
    paths += list((RESEARCH / "solver").glob("*.py")) + list(OFFICIAL.glob("*.py"))
    paths += [RESEARCH / "探索" / name for name in ("advanced_solve.py", "operation_heft_probe.py", "partition_candidates.py")]
    return {str(p.resolve()): digest(p) for p in sorted(paths)}


def parse_numbers(raw, maximum, *, allow_all=False):
    if allow_all and raw.strip().lower() == "all":
        return list(range(1, maximum + 1))
    result = []
    for part in raw.split(","):
        part = part.strip()
        if allow_all and part.startswith("case_"):
            part = part[5:]
        if not part.isascii() or not part.isdecimal() or not 1 <= int(part) <= maximum:
            raise ValueError("expected comma-separated integers 1..{}".format(maximum))
        number = int(part)
        if number not in result:
            result.append(number)
    if not result:
        raise ValueError("empty selection")
    return result


def _contained(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if root not in path.parents:
        raise ValueError("artifact path escapes batch run directory: {}".format(path))
    return path


def _slot_key(case, problem, cores):
    return "{}_p{}_n{}".format(case, problem, cores)


def _signature(settings, case, problem, cores, manifest, incumbent=None):
    graph = Path(settings["data_dir"]) / (case + ".json")
    return {"schema_version": 1, "graph_path": str(graph.resolve()),
            "graph_sha256": manifest["graphs_sha256"][case],
            "config_path": settings["config"], "config_sha256": manifest["config_sha256"],
            "source_sha256": manifest["source_sha256"], "problem": problem, "num_cores": cores,
            "profile": settings["profile"], "seed": settings["seed"], "budgets": settings["budgets"],
            "max_rounds": settings["max_rounds"], "max_evaluations": settings["max_evaluations"],
            "timeout": settings["timeout"], "wall_budget": settings["wall_budget"],
            "evaluation_dir": settings.get("evaluation_dir"),
            "initial_plan_sha256": object_digest(read_json(incumbent)) if incumbent else None,
            "initial_plan_file_sha256": digest(incumbent) if incumbent else None,
            "initial_plan_path": str(Path(incumbent).resolve()) if incumbent else None}


def build_command(settings, signature, attempt, *, solve_entry=None, supervisor_entry=None):
    supervised = settings["wall_budget"] is not None
    entry = (supervisor_entry or HERE / "supervisor.py") if supervised else (solve_entry or HERE / "solve.py")
    if not Path(entry).is_file():
        raise ValueError("entry point is not ready: {}".format(entry))
    args = [sys.executable, "-B", str(entry), signature["graph_path"],
            "-n", str(signature["num_cores"]), "-p", str(signature["problem"]),
            "--config", signature["config_path"], "--run-dir", str(attempt),
            "-o", str(attempt / "best.plan.json"), "--profile", settings["profile"],
            "--seed", str(settings["seed"]), "--max-rounds", str(settings["max_rounds"]),
            "--max-evaluations", str(settings["max_evaluations"])]
    if settings["timeout"] is not None:
        args += ["--timeout", str(settings["timeout"])]
    for stage, cap in settings["budgets"].items():
        args += ["--{}-cap".format(stage), str(cap)]
    if signature["initial_plan_path"]:
        args += ["--incumbent-plan", signature["initial_plan_path"]]
    if supervised:
        args += ["--wall-budget", str(settings["wall_budget"])]
    if settings.get("evaluation_dir"):
        args += ["--evaluation-dir", settings["evaluation_dir"]]
    return args


def verify_summary(attempt, signature):
    """Validate evidence before either accepting a new result or resuming it."""
    attempt = Path(attempt)
    result = read_json(attempt / "summary.json")
    if (result.get("best") is None and result.get("status") == "no_feasible_result"
            and result.get("completed") is False and result.get("state") in ("supervisor_timeout", "child_failed")
            and "source_sha256" not in result):
        # A child killed before a usable checkpoint has no certifiable result.
        # Preserve this failure, never reuse it as a completed search, and let
        # resume launch a separate new attempt under the frozen launch manifest.
        if not (attempt / "supervisor.json").is_file() or read_json(attempt / "supervisor.json") != result.get("supervisor"):
            raise ValueError("minimal failure summary lacks matching supervisor evidence")
        return {**result, "evaluated_count": None, "summary_validation": "minimal_failure_no_result"}
    for field in ("graph_sha256", "config_sha256", "source_sha256", "problem", "num_cores",
                  "profile", "seed", "budgets", "max_rounds", "initial_plan_sha256"):
        if result.get(field) != signature[field]:
            raise ValueError("summary {} does not match frozen slot signature".format(field))
    if result.get("total_evaluations") != signature["max_evaluations"]:
        raise ValueError("summary total evaluation budget mismatch")
    if signature["timeout"] is not None and result.get("per_evaluation_timeout") != signature["timeout"]:
        raise ValueError("summary per-evaluation timeout mismatch")
    if type(result.get("completed")) is not bool:
        raise ValueError("summary must explicitly report completed=true/false")
    if result["completed"] and result.get("state") != "finished":
        raise ValueError("completed result is not in finished state")
    if result.get("source_changed") is True or result.get("source_hashes_unchanged") is False:
        raise ValueError("solver reported source changes")
    evaluations = result.get("evaluations")
    if not isinstance(evaluations, list) or result.get("evaluated_count") != len(evaluations):
        raise ValueError("summary evaluation count mismatch")
    if signature["max_evaluations"] is not None and len(evaluations) > signature["max_evaluations"]:
        raise ValueError("summary exceeds total evaluation cap")
    if signature["initial_plan_sha256"] is not None:
        initial = [row for row in evaluations if row.get("stage") == "initial"]
        if len(initial) != 1 or initial[0].get("plan_sha256") != signature["initial_plan_sha256"]:
            raise ValueError("warm incumbent was not charged as exactly one initial evaluation")
    best = result.get("best")
    if result.get("status") not in ("success", "no_feasible_result") or bool(best) != (result["status"] == "success"):
        raise ValueError("summary feasibility/status mismatch")
    rows = evaluations + ([best] if best else [])
    official_sources = {Path(p).name: h for p, h in signature["source_sha256"].items()
                        if Path(p).parent.resolve() == OFFICIAL.resolve()}
    checked_paths = set()
    for row in rows:
        record = row.get("record", {})
        if record.get("status") != "success":
            if row is best:
                raise ValueError("best does not have an official success record")
            continue
        hashes = record.get("hashes", {})
        if (record.get("problem") != signature["problem"]
                or hashes.get("graph_sha256") != signature["graph_sha256"]
                or hashes.get("config_sha256") != signature["config_sha256"]
                or hashes.get("official_py_sha256") != official_sources):
            raise ValueError("official success record input/source signature mismatch")
        plan_path, result_path = Path(record["plan_path"]), Path(record["result_path"])
        if digest(plan_path) != hashes.get("plan_sha256") or digest(result_path) != record.get("result_sha256"):
            raise ValueError("official plan/result file SHA256 mismatch")
        evaluated_plan = read_json(plan_path)
        if row.get("plan_sha256") is not None and row["plan_sha256"] != object_digest(evaluated_plan):
            raise ValueError("evaluation row plan hash mismatch")
        if row is best and object_digest(best.get("plan")) != object_digest(evaluated_plan):
            raise ValueError("selected plan is not its officially evaluated plan")
        if str(result_path) not in checked_paths:
            with gzip.open(result_path, "rt", encoding="utf-8") as stream:
                official = json.load(stream)
            if (official.get("makespan") != record.get("metrics", {}).get("makespan")
                    or official.get("num_cores") != signature["num_cores"]):
                raise ValueError("official raw result and summary metrics disagree")
            checked_paths.add(str(result_path))
    if best:
        output = read_json(attempt / "best.plan.json")
        if object_digest(output) != object_digest(best["plan"]):
            raise ValueError("published best.plan.json differs from verified best")
    return result


def _result_row(case, problem, cores, attempt, result, *, returncode=None, reused=False):
    feasible = result.get("best") is not None
    completed = result["completed"]
    outcome = ("completed" if completed else "unfinished") + ("_feasible" if feasible else "_no_feasible")
    best = result.get("best")
    return {"slot": _slot_key(case, problem, cores), "case": case, "problem": problem, "num_cores": cores,
            "outcome": outcome, "feasible": feasible, "search_completed": completed,
            "attempt_dir": str(attempt), "summary_path": str(attempt / "summary.json"),
            "plan_path": str(attempt / "best.plan.json") if feasible else None,
            "makespan": best["record"]["metrics"]["makespan"] if feasible else None,
            "evaluated_count": result["evaluated_count"], "official_calls": result.get("official_calls"),
            "returncode": returncode, "reused": reused, "state": result.get("state"),
            "stop_reason": result.get("stop_reason"),
            "summary_validation": result.get("summary_validation", "full_hash_and_result_verification")}


def run_slot(settings, manifest, case, problem, cores, out, *, incumbent=None, resume=False,
             runner=None, solve_entry=None, supervisor_entry=None):
    runner = runner or subprocess.run
    slot = _slot_key(case, problem, cores)
    root = _contained(out / "slots" / case / ("p{}_n{}".format(problem, cores)), out)
    root.mkdir(parents=True, exist_ok=True)
    signature = _signature(settings, case, problem, cores, manifest, incumbent)
    launches = sorted(root.glob("attempt_*.launch.json"))
    if launches and not resume:
        raise ValueError("slot already has attempts; use --resume")
    next_number, prior = 1, []
    for launch_path in launches:
        launch = read_json(launch_path)
        if launch["signature"] != signature:
            raise ValueError("existing slot attempt does not match current frozen signature")
        attempt = _contained(Path(launch["attempt_dir"]), out)
        next_number = max(next_number, launch["attempt_number"] + 1)
        if (attempt / "summary.json").exists():
            result = verify_summary(attempt, signature)
            row = _result_row(case, problem, cores, attempt, result, reused=True)
            prior.append(row)
        else:
            prior.append({"attempt_dir": str(attempt), "outcome": "missing_summary", "feasible": False, "search_completed": False})
    # Reuse only a fully verified successful completed search. An unfinished
    # timeout is useful evidence, but not permission to call the search done.
    reusable = [r for r in prior if r["feasible"] and r["search_completed"]]
    if reusable:
        selected = min(reusable, key=lambda r: r["makespan"])
        return {**selected, "prior_attempts": prior, "attempt_count": len(launches)}
    attempt = _contained(root / ("attempt_{:04d}".format(next_number)), out)
    if attempt.exists():
        raise ValueError("untracked attempt path exists; refusing overwrite")
    if source_hashes() != manifest["source_sha256"] or digest(Path(__file__).resolve()) != manifest["batch_py_sha256"]:
        raise ValueError("source changed before slot launch")
    command = build_command(settings, signature, attempt, solve_entry=solve_entry, supervisor_entry=supervisor_entry)
    launch_path = root / ("attempt_{:04d}.launch.json".format(next_number))
    launch = {"signature": signature, "command": command, "attempt_number": next_number,
              "attempt_dir": str(attempt), "created_unix": time.time(),
              "warm_initial_evaluation_is_charged": incumbent is not None}
    atomic_json(launch_path, launch)
    stdout = root / ("attempt_{:04d}.stdout.log".format(next_number))
    stderr = root / ("attempt_{:04d}.stderr.log".format(next_number))
    started = time.monotonic()
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with stdout.open("w", encoding="utf-8") as out_log, stderr.open("w", encoding="utf-8") as err_log:
        process = runner(command, stdout=out_log, stderr=err_log, env=env, check=False)
    launch.update({"returncode": process.returncode, "elapsed_seconds": time.monotonic() - started})
    atomic_json(launch_path, launch)
    if source_hashes() != manifest["source_sha256"] or digest(Path(__file__).resolve()) != manifest["batch_py_sha256"]:
        raise ValueError("source changed during slot execution")
    result = verify_summary(attempt, signature)
    row = _result_row(case, problem, cores, attempt, result, returncode=process.returncode)
    row.update({"prior_attempts": prior, "attempt_count": next_number,
                "warm_initial_evaluation_is_charged": incumbent is not None,
                "stdout_path": str(stdout), "stderr_path": str(stderr)})
    atomic_json(root / "slot.json", row)
    return row


def run_batch(settings, out, *, resume=False, runner=None, solve_entry=None, supervisor_entry=None):
    out = Path(out).resolve()
    if out == DATA.parent.resolve() or DATA.parent.resolve() in out.parents:
        raise ValueError("batch artifacts must be outside official attachments")
    entry = (supervisor_entry or HERE / "supervisor.py") if settings["wall_budget"] is not None else (solve_entry or HERE / "solve.py")
    if not Path(entry).is_file():
        raise ValueError("entry point is not ready: {}".format(entry))
    graph_hashes = {"case_{:03d}".format(c): digest(Path(settings["data_dir"]) / ("case_{:03d}.json".format(c)))
                    for c in settings["cases"]}
    expected = {"schema_version": 1, "settings": settings, "graphs_sha256": graph_hashes,
                "config_sha256": digest(settings["config"]), "source_sha256": source_hashes(),
                "batch_py_sha256": digest(Path(__file__).resolve()),
                "python": sys.version, "python_executable": sys.executable,
                "warm_p2_scope": "exploratory_not_matched_ablation" if settings["warm_p2"] else "none"}
    manifest_path = out / "manifest.json"
    if manifest_path.exists():
        if not resume:
            raise ValueError("batch directory already exists; use --resume")
        if read_json(manifest_path) != expected:
            raise ValueError("batch manifest input/config/source/settings mismatch; use a new run directory")
    elif out.exists() and any(out.iterdir()):
        raise ValueError("nonempty batch directory has no matching manifest")
    else:
        out.mkdir(parents=True, exist_ok=True)
        atomic_json(manifest_path, expected)
    manifest = expected
    rows, lock = {}, threading.Lock()
    slots = [_slot_key("case_{:03d}".format(c), p, n) for c in settings["cases"]
             for n in settings["cores"] for p in settings["problems"]]

    def publish(row):
        with lock:
            rows[row["slot"]] = row
            atomic_json(out / "progress.json", {"planned_slots": len(slots), "reported_slots": len(rows),
                        "pending_slots": [s for s in slots if s not in rows], "slots": list(rows.values())})

    def graph_job(case_number):
        case = "case_{:03d}".format(case_number)
        local = {}
        problems = sorted(settings["problems"]) if settings["warm_p2"] else settings["problems"]
        for cores in settings["cores"]:
            for problem in problems:
                incumbent = None
                p2 = local.get((2, cores))
                if problem == 3 and settings["warm_p2"] and p2 and p2.get("feasible"):
                    incumbent = p2["plan_path"]
                try:
                    row = run_slot(settings, manifest, case, problem, cores, out, incumbent=incumbent,
                        resume=resume, runner=runner, solve_entry=solve_entry, supervisor_entry=supervisor_entry)
                except Exception as error:
                    row = {"slot": _slot_key(case, problem, cores), "case": case, "problem": problem,
                           "num_cores": cores, "outcome": "resume_rejected" if resume else "slot_failed",
                           "slot_dir": str(out / "slots" / case / ("p{}_n{}".format(problem, cores))),
                           "feasible": False, "search_completed": False, "error": str(error),
                           "traceback": traceback.format_exc(), "reused": False}
                local[problem, cores] = row
                publish(row)
        return local

    with ThreadPoolExecutor(max_workers=settings["workers"]) as pool:
        futures = {pool.submit(graph_job, c): c for c in settings["cases"]}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as error:
                case = "case_{:03d}".format(futures[future])
                for cores in settings["cores"]:
                    for problem in settings["problems"]:
                        slot = _slot_key(case, problem, cores)
                        if slot not in rows:
                            publish({"slot": slot, "case": case, "problem": problem, "num_cores": cores,
                                "outcome": "case_worker_failed", "feasible": False, "search_completed": False,
                                "error": str(error), "traceback": traceback.format_exc(), "reused": False})
    ordered = [rows[slot] for slot in slots]
    completed = all(r["search_completed"] for r in ordered)
    final = {"schema_version": 1, "planned_slots": len(slots), "reported_slots": len(ordered),
             "all_searches_completed": completed, "all_slots_feasible": all(r["feasible"] for r in ordered),
             "feasible_count": sum(r["feasible"] for r in ordered),
             "completed_count": sum(r["search_completed"] for r in ordered),
             "reused_count": sum(r.get("reused", False) for r in ordered),
             "slots": ordered, "source_hashes_unchanged": (source_hashes() == manifest["source_sha256"]
                  and digest(Path(__file__).resolve()) == manifest["batch_py_sha256"]),
             "warm_p2_scope": manifest["warm_p2_scope"], "budget_applies_per_attempt": True}
    atomic_json(out / "summary.json", final)
    return final


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="all")
    parser.add_argument("--problems", default="1,2,3")
    parser.add_argument("--cores", default="1,2,3,4,5")
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--profile", choices=("component", "operation", "trace", "cache", "full"), default="full")
    parser.add_argument("--seed", type=int, default=17)
    for stage, cap in (("component", 6), ("operation", 12), ("trace", 24), ("cache", 24)):
        parser.add_argument("--{}-cap".format(stage), type=int, default=cap)
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--max-evaluations", type=int, default=67)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--wall-budget", type=float)
    parser.add_argument("--evaluation-dir", type=Path, help="Shared exact-plan cache; logical trials remain charged; not cold walltime comparison")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--warm-p2", action="store_true")
    parser.add_argument("--exploratory", action="store_true", help="labels warm-start use outside full profile as exploratory")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = {"cases": parse_numbers(args.cases, 100, allow_all=True),
            "problems": parse_numbers(args.problems, 3), "cores": parse_numbers(args.cores, 5),
            "data_dir": str(args.data_dir.resolve()), "config": str((args.config or args.data_dir / "config.txt").resolve()),
            "profile": args.profile, "seed": args.seed, "budgets": {s: getattr(args, s + "_cap") for s in ("component", "operation", "trace", "cache")},
            "max_rounds": args.max_rounds, "max_evaluations": args.max_evaluations,
            "timeout": args.timeout, "wall_budget": args.wall_budget, "workers": args.workers,
            "evaluation_dir": str(args.evaluation_dir.resolve()) if args.evaluation_dir else None,
            "warm_p2": args.warm_p2, "exploratory": args.exploratory}
        if args.workers < 1 or args.max_rounds < 1 or args.max_evaluations < 1 or any(v < 0 for v in settings["budgets"].values()):
            raise ValueError("workers/rounds/evaluations must be positive and stage caps nonnegative")
        if any(v is not None and (not math.isfinite(v) or v <= 0) for v in (args.timeout, args.wall_budget)):
            raise ValueError("timeouts must be finite and positive")
        if args.profile == "cache" and settings["problems"] != [3]:
            raise ValueError("cache profile only supports P3")
        if args.warm_p2 and (2 not in settings["problems"] or 3 not in settings["problems"]):
            raise ValueError("--warm-p2 requires both P2 and P3 in this batch")
        if args.warm_p2 and args.profile != "full" and not args.exploratory:
            raise ValueError("--warm-p2 is only allowed for full profile or explicit exploratory runs")
        if args.evaluation_dir and (args.evaluation_dir.resolve() == DATA.parent.resolve() or DATA.parent.resolve() in args.evaluation_dir.resolve().parents):
            raise ValueError("shared evaluation directory must be outside official attachments")
        result = run_batch(settings, args.run_dir, resume=args.resume)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps({"summary": str(args.run_dir.resolve() / "summary.json"),
        "reported_slots": result["reported_slots"], "feasible_count": result["feasible_count"],
        "completed_count": result["completed_count"], "reused_count": result["reused_count"]}, ensure_ascii=False))
    return 0 if result["all_searches_completed"] and result["all_slots_feasible"] and result["source_hashes_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
