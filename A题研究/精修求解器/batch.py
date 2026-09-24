#!/usr/bin/env python3
"""Fresh v3 portfolio batches: parallel graphs, sequential slots, strict resume.

Never imports historical scores or invokes the official evaluator directly.
Completed evidence must verify before resume; other attempts remain immutable.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import gzip
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import traceback

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
sys.path[:0] = [str(HERE), str(RESEARCH)]
import controller
from solver.common import DATA, OFFICIAL, atomic_json, digest, object_digest, read_json
from solver.graph_ir import GraphIR
from solver.plan import validate_plan

CAP_DEFAULTS = dict(component=6, operation=12, selective=18, wcc=9, trace=30, cache=18)


class IntegrityError(ValueError):
    """Frozen sources/inputs/parameters changed; never repair by silent resume."""


def source_hashes():
    # Explicit execution closure supplied by controller; excludes this runner.
    # batch.py's own hash is a separate manifest field, not recursive closure.
    return controller.source_hashes()


def parse_numbers(raw, maximum, *, allow_all=False):
    if allow_all and raw.strip().lower() == "all":
        return list(range(1, maximum + 1))
    result = []
    for part in raw.split(","):
        part = part.strip()
        if allow_all and part.startswith("case_"):
            part = part[5:]
        if not part.isascii() or not part.isdecimal() or not 1 <= int(part) <= maximum:
            raise ValueError(f"expected comma-separated integers in 1..{maximum}")
        if int(part) not in result:
            result.append(int(part))
    if not result:
        raise ValueError("empty selection")
    return result


def _key(case, problem, cores):
    return f"{case}_p{problem}_n{cores}"


def _objective(record):
    metrics = record["metrics"]
    t = metrics["makespan"]
    b = metrics["data_movement_bytes"]["added_copy_bytes"]
    if type(t) not in (int, float) or not math.isfinite(t) or t < 0 or type(b) is not int:
        raise ValueError("invalid official objective")
    return t, b


def _effective_caps(caps, problem):
    return {s: n if (s == "component" or (problem == 1 and s == "selective") or
        (problem in (2, 3) and s in ("operation", "wcc", "trace")) or
        (problem == 3 and s == "cache")) else 0 for s, n in caps.items()}


def validate_settings(settings):
    for name, limit in (("cases", 100), ("problems", 3), ("cores", 5)):
        v = settings[name]
        if not isinstance(v, list) or not v or len(v) != len(set(v)) or any(type(x) is not int or not 1 <= x <= limit for x in v):
            raise ValueError(f"invalid {name} selection")
    if set(settings["caps"]) != set(CAP_DEFAULTS) or any(type(v) is not int or v < 0 for v in settings["caps"].values()):
        raise ValueError("all six stage caps must be nonnegative integers")
    if any(type(settings[k]) is not int or settings[k] < 1 for k in ("workers", "round_width", "max_rounds", "max_evaluations")):
        raise ValueError("workers, round width, rounds and evaluation cap must be positive")
    if type(settings["seed"]) is not int or settings["wcc_policy"] not in ("mixed", "protected", "unrestricted"):
        raise ValueError("invalid seed or WCC policy")
    timeout = settings["timeout"]
    if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout must be finite positive or None for graph-size rule")
    if settings.get("incumbent_plan") and len(settings["cases"]) * len(settings["problems"]) * len(settings["cores"]) != 1:
        raise ValueError("explicit incumbent is allowed only for one selected slot")


def _outside_official(path):
    path = Path(path).expanduser().resolve()
    if path == OFFICIAL.parent.resolve() or OFFICIAL.parent.resolve() in path.parents:
        raise ValueError("batch/evaluation outputs must stay outside official attachments")
    return path


def make_manifest(settings, solve_entry):
    validate_settings(settings)
    graph_hashes, counts = {}, {}
    for number in settings["cases"]:
        case = f"case_{number:03d}"
        path = Path(settings["data_dir"]) / (case + ".json")
        graph_hashes[case] = digest(path)
        graph = read_json(path)
        counts[case] = sum(o["op"] not in ("COPY_IN", "COPY_OUT") for o in graph["ops"])
    incumbent = settings.get("incumbent_plan")
    return {"schema_version": 1, "batch_protocol": "fresh_v3_portfolio",
        "settings": settings, "graphs_sha256": graph_hashes, "compute_ops_by_case": counts,
        "config_sha256": digest(settings["config"]), "source_sha256": source_hashes(),
        "batch_py_sha256": digest(__file__), "solve_entry": str(Path(solve_entry).resolve()),
        "solve_entry_sha256": digest(solve_entry), "python": sys.version,
        "python_executable": str(Path(sys.executable).resolve()),
        "incumbent_file_sha256": digest(incumbent) if incumbent else None,
        "incumbent_plan_sha256": object_digest(read_json(incumbent)) if incumbent else None,
        "scope": "v3 stronger-budget fresh search; not formal_v2 equal-budget ablation",
        "timeout_rule": "non-COPY ops <=10000:60s; otherwise180s; explicit timeout overrides",
        "budget_applies_per_attempt": True,
        "walltime_scope": "shared-cache/concurrent observed time is not cold-cache fair speed",
        "initial_scope": "explicit_single_slot_incumbent" if incumbent else "from_scratch_no_historical_initials"}


def _assert_frozen(manifest, case=None):
    if source_hashes() != manifest["source_sha256"] or digest(__file__) != manifest["batch_py_sha256"]:
        raise IntegrityError("batch/controller/dependency sources changed")
    if digest(manifest["solve_entry"]) != manifest["solve_entry_sha256"]:
        raise IntegrityError("solve entry changed")
    settings = manifest["settings"]
    if digest(settings["config"]) != manifest["config_sha256"]:
        raise IntegrityError("config changed")
    selected = [case] if case else manifest["graphs_sha256"]
    for name in selected:
        if digest(Path(settings["data_dir"]) / (name + ".json")) != manifest["graphs_sha256"][name]:
            raise IntegrityError("graph changed: " + name)
    if settings.get("incumbent_plan") and digest(settings["incumbent_plan"]) != manifest["incumbent_file_sha256"]:
        raise IntegrityError("explicit incumbent changed")


def slot_signature(manifest, case, problem, cores):
    s = manifest["settings"]
    timeout = s["timeout"] if s["timeout"] is not None else (60 if manifest["compute_ops_by_case"][case] <= 10000 else 180)
    return {"graph_path": str((Path(s["data_dir"]) / (case + ".json")).resolve()),
        "graph_sha256": manifest["graphs_sha256"][case], "config_path": s["config"],
        "config_sha256": manifest["config_sha256"], "source_sha256": manifest["source_sha256"],
        "problem": problem, "num_cores": cores, "seed": s["seed"], "requested_caps": s["caps"],
        "effective_caps": _effective_caps(s["caps"], problem), "round_width": s["round_width"],
        "max_rounds": s["max_rounds"], "max_evaluations": s["max_evaluations"],
        "timeout": timeout, "wcc_policy": s["wcc_policy"],
        "incumbent_path": s.get("incumbent_plan"), "incumbent_file_sha256": manifest["incumbent_file_sha256"],
        "incumbent_plan_sha256": manifest["incumbent_plan_sha256"],
        "python": manifest["python"], "python_executable": manifest["python_executable"],
        "shared_evaluation_dir": s.get("evaluation_dir")}


def build_command(manifest, signature, attempt):
    command = [sys.executable, "-B", manifest["solve_entry"], signature["graph_path"],
        "-n", str(signature["num_cores"]), "-p", str(signature["problem"]),
        "--config", signature["config_path"], "--run-dir", str(attempt),
        "-o", str(attempt / "best.plan.json"), "--seed", str(signature["seed"]),
        "--round-width", str(signature["round_width"]), "--max-rounds", str(signature["max_rounds"]),
        "--max-evaluations", str(signature["max_evaluations"]), "--timeout", str(signature["timeout"]),
        "--wcc-policy", signature["wcc_policy"]]
    for stage, cap in signature["requested_caps"].items():
        command += [f"--{stage}-cap", str(cap)]
    if signature["shared_evaluation_dir"]:
        command += ["--evaluation-dir", signature["shared_evaluation_dir"]]
    if signature["incumbent_path"]:
        command += ["--incumbent-plan", signature["incumbent_path"]]
    return command


def verify_summary(attempt, signature):
    """Independently verify all success file hashes and selected raw execution."""
    attempt = Path(attempt).resolve()
    result = read_json(attempt / "summary.json")
    fields = ("graph_path", "graph_sha256", "config_path", "config_sha256", "source_sha256",
        "problem", "num_cores", "seed", "requested_caps", "effective_caps", "round_width",
        "max_rounds", "max_evaluations", "timeout", "wcc_policy", "incumbent_path",
        "incumbent_file_sha256", "incumbent_plan_sha256", "python")
    for field in fields:
        if result.get(field) != signature[field]:
            raise ValueError("summary differs from frozen signature: " + field)
    if (result.get("completed") is not True or result.get("status") != "success"
            or result.get("state") != "finished" or result.get("source_and_input_hashes_verified") is not True
            or result.get("test_hooks_used") is not False):
        raise ValueError("only completed, source-verified real success can be reused")
    expected_evaluation_dir = signature["shared_evaluation_dir"] or str(attempt / "evaluations")
    if result.get("evaluation_dir") != expected_evaluation_dir:
        raise ValueError("summary evaluation directory mismatch")
    rows = result.get("evaluations")
    if (not isinstance(rows, list) or type(result.get("logical_calls")) is not int
            or len(rows) != result["logical_calls"] or not 1 <= len(rows) <= signature["max_evaluations"]):
        raise ValueError("summary logical call ledger/cap mismatch")
    if any(r.get("state") != "returned" or r.get("index") != i for i, r in enumerate(rows)):
        raise ValueError("completed search has pending or misindexed trials")
    if result.get("pending_calls") != 0 or result.get("returned_calls") != len(rows):
        raise ValueError("returned/pending call counts mismatch")
    statuses = dict(Counter(r["record"]["status"] for r in rows))
    if result.get("status_counts") != statuses or result.get("stage_calls") != dict(Counter(r["stage"] for r in rows)):
        raise ValueError("stage/status ledger counts mismatch")
    if result.get("cache_hits") != sum(bool(r["record"].get("cache_hit")) for r in rows):
        raise ValueError("cache hit count mismatch")
    initial = [r for r in rows if r["stage"] == "initial"]
    if signature["incumbent_plan_sha256"] is None:
        if initial: raise ValueError("from-scratch search unexpectedly used an initial plan")
    elif len(initial) != 1 or initial[0]["plan_sha256"] != signature["incumbent_plan_sha256"]:
        raise ValueError("explicit incumbent was not charged exactly once")
    source = signature["source_sha256"]
    official_sources = {Path(p).name: h for p, h in source.items() if Path(p).parent.resolve() == OFFICIAL.resolve()}
    successes = []
    for row in rows:
        trial_plan = Path(row["plan_path"])
        if attempt / "trials" not in trial_plan.resolve().parents or digest(trial_plan) != row["plan_file_sha256"]:
            raise ValueError("trial plan file path/hash mismatch")
        if object_digest(read_json(trial_plan)) != row["plan_sha256"]:
            raise ValueError("trial plan content hash mismatch")
        record = row["record"]
        if record["status"] != "success":
            continue
        expected = {"graph_sha256": signature["graph_sha256"], "config_sha256": signature["config_sha256"],
            "plan_sha256": row["plan_sha256"], "problem": signature["problem"],
            "official_py_sha256": official_sources,
            "wrapper_sha256": source[str((RESEARCH / "solver/evaluator.py").resolve())],
            "worker_sha256": source[str((RESEARCH / "solver/eval_worker.py").resolve())],
            "python": signature["python"], "python_executable": signature["python_executable"]}
        if (record.get("problem") != signature["problem"] or record.get("returncode") != 0
                or record.get("metrics", {}).get("num_cores") != signature["num_cores"]
                or any(record.get("hashes", {}).get(k) != v for k, v in expected.items())):
            raise ValueError("official success source/input hashes mismatch")
        if digest(record["plan_path"]) != row["plan_sha256"] or digest(record["result_path"]) != record["result_sha256"]:
            raise ValueError("official plan/result bytes mismatch")
        if object_digest(read_json(record["plan_path"])) != row["plan_sha256"]:
            raise ValueError("official plan content mismatch")
        _objective(record)
        successes.append(row)
    best = result.get("best")
    if not best or not successes or best.get("index") not in [r["index"] for r in successes]:
        raise ValueError("best is not a successful ledger trial")
    selected = rows[best["index"]]
    if best.get("record") != selected["record"] or best.get("plan_sha256") != selected["plan_sha256"]:
        raise ValueError("best disagrees with ledger")
    if _objective(best["record"]) != min(_objective(r["record"]) for r in successes):
        raise ValueError("selected objective is not the best successful trial")
    output = attempt / "best.plan.json"
    if result.get("output") != str(output) or digest(output) != result.get("output_sha256"):
        raise ValueError("published plan path/file hash mismatch")
    plan = read_json(output)
    if object_digest(plan) != best["plan_sha256"] or object_digest(best.get("plan")) != best["plan_sha256"]:
        raise ValueError("published plan differs from evaluated best")
    ir = GraphIR.from_path(signature["graph_path"])
    validate_plan(ir, plan)
    if len(plan["core_schedules"]) != signature["num_cores"]:
        raise ValueError("published plan core count mismatch")
    with gzip.open(best["record"]["result_path"], "rt", encoding="utf-8") as stream:
        raw = json.load(stream)
    expected_raw = {"scene": "A" if signature["problem"] == 1 else "B", "num_cores": signature["num_cores"],
        "bandwidth_bytes_per_cycle": 60, "capacity_bytes": {"L1": 524288, "UB": 131072},
        "input_plan": Path(best["record"]["plan_path"]).name}
    if signature["problem"] == 1:
        expected_raw.update(task_cross_core_wait_cycles=1000, task_same_core_wait_cycles=100)
    else:
        expected_raw.update(cross_core_copy_delay_cycles=500)
    if signature["problem"] == 3:
        expected_raw.update(problem=3, cache_mode="read_only", cache_capacity_bytes=1048576, cache_bandwidth_bytes_per_cycle=250)
    if any(raw.get(k) != v for k, v in expected_raw.items()):
        raise ValueError("raw official scene/config/provenance mismatch")
    if (not isinstance(raw.get("input_graph"), str) or not raw["input_graph"] or
            (not best["record"].get("cache_hit") and raw["input_graph"] != Path(signature["graph_path"]).name)):
        raise ValueError("raw graph label mismatch without exact-cache reuse")
    if raw.get("makespan") != _objective(best["record"])[0] or raw.get("data_movement_bytes") != best["record"]["metrics"]["data_movement_bytes"]:
        raise ValueError("raw official score/bytes differ from summary")
    mapping = {int(o): sg for o, sg in plan["node_to_subgraph"].items()}
    cores = {sg: k for k, seq in enumerate(plan["core_schedules"]) for sg in seq}
    timelines = raw["per_core_timeline"]
    if len(timelines) != signature["num_cores"] or {c["core_id"] for c in timelines} != set(range(signature["num_cores"])):
        raise ValueError("raw timeline core coverage mismatch")
    found, finish = set(), 0
    for core in timelines:
        k = core["core_id"]
        order = ([t["subgraph_id"] for t in core["tasks"]] if signature["problem"] == 1
                 else [sg for t in core["tasks"] for sg in t["subgraph_ids"]])
        if order != plan["core_schedules"][k]:
            raise ValueError("raw Task/SG order differs from published plan")
        for op in core["ops"]:
            a, b = op["start"], op["end"]
            if any(type(x) not in (int, float) or not math.isfinite(x) for x in (a, b)) or a < 0 or b < a:
                raise ValueError("invalid raw timeline time")
            finish = max(finish, b)
            o = op["op_id"]
            if o not in mapping: continue
            original, sg = ir.ops[o], mapping[o]
            if (o in found or cores[sg] != k or (op["task_id"] if signature["problem"] == 1 else op["subgraph_id"]) != sg
                    or op["op"] != original["op"] or op["pipe"] != original["pipe"] or b - a != max(1, original["cycles"])):
                raise ValueError("raw original compute identity/assignment/duration mismatch")
            found.add(o)
    if found != set(mapping) or finish != raw["makespan"]:
        raise ValueError("raw compute coverage/final makespan mismatch")
    # The frozen solver verifier additionally audits every successful trial's
    # complete raw execution and incumbent acceptance, rather than just winner.
    # require_completed=False permits evidence retention for a requires_review
    # search; the batch still refuses to count that search as complete.
    controller.verify_summary(attempt / "summary.json",
        expected_settings={k: signature[k] for k in fields}, require_completed=False)
    # Official candidate failures/timeouts are normal feedback. Generator
    # exceptions or structural rejections indicate an implementation issue and
    # must not silently mark a formal experimental slot complete.
    return {**result, "requires_review": bool(result.get("requires_review") or result.get("generation_failures")
        or result.get("rejected_candidates") or result.get("controller_error"))}


def _ledger_hint(attempt):
    """Retain diagnostic counts on failures; do not certify an unfinished score."""
    for name in ("summary.json", "checkpoint.json"):
        try:
            s = read_json(Path(attempt) / name)
            n = s.get("logical_calls")
            return {"reported_logical_calls": n if type(n) is int and n >= 0 else None,
                "reported_status_counts": s.get("status_counts"), "reported_state": s.get("state"),
                "source_status": s.get("source_and_input_hashes_verified"), "ledger_hint_verified": False}
        except (OSError, ValueError, TypeError):
            pass
    return {"reported_logical_calls": None, "ledger_hint_verified": False, "source_status": None}


def _pending(case, problem, cores):
    return {"slot": _key(case, problem, cores), "case": case, "problem": problem, "num_cores": cores,
        "outcome": "pending", "state": "not_started", "feasible": False, "search_completed": False,
        "reused": False, "logical_calls": None, "source_status": "not_checked"}


def _success(case, problem, cores, attempt, result, reused):
    review = result.get("requires_review", False)
    return {**_pending(case, problem, cores), "outcome": "failed" if review else "completed_feasible",
        "state": "requires_review" if review else result["state"], "requires_review": review,
        "feasible": True, "search_completed": not review, "reused": reused, "attempt_dir": str(attempt),
        "summary_path": str(attempt / "summary.json"), "summary_sha256": digest(attempt / "summary.json"),
        "plan_path": result["output"], "plan_file_sha256": result["output_sha256"],
        "plan_sha256": result["best"]["plan_sha256"], "makespan": _objective(result["best"]["record"])[0],
        "added_copy_bytes": _objective(result["best"]["record"])[1], "logical_calls": result["logical_calls"],
        "cache_hits": result["cache_hits"], "confirmed_worker_calls": result.get("confirmed_worker_calls"),
        "status_counts": result["status_counts"], "stage_calls": result["stage_calls"],
        "source_status": "verified", "stop_reason": result.get("stop_reason"),
        "generation_failure_count": len(result.get("generation_failures", [])),
        "structural_rejection_count": len(result.get("rejected_candidates", [])),
        "summary_validation": "all_success_file_hashes_and_selected_raw_timeline_verified"}


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def run_slot(manifest, case, problem, cores, out, *, resume=False, runner=None):
    settings = manifest["settings"]
    root = out / "slots" / case / f"p{problem}_n{cores}"
    root.mkdir(parents=True, exist_ok=True)
    if root.resolve() != root or out not in root.parents:
        raise IntegrityError("slot path escapes its batch directory")
    signature = slot_signature(manifest, case, problem, cores)
    launches = sorted(root.glob("attempt_*.launch.json"))
    directories = sorted(p for p in root.glob("attempt_*") if p.is_dir())
    if (launches or directories) and not resume:
        raise IntegrityError("slot already has attempts; use --resume")
    tracked = {p.name.removesuffix(".launch.json") for p in launches}
    if any(p.name not in tracked for p in directories):
        raise IntegrityError("untracked attempt directory; refusing overwrite")
    prior, next_number = [], 1
    for path in launches:
        launch = read_json(path)
        number = launch["attempt_number"]
        attempt = root / f"attempt_{number:04d}"
        if (launch.get("signature") != signature or launch.get("signature_sha256") != object_digest(signature)
                or launch.get("attempt_dir") != str(attempt) or path.name != attempt.name + ".launch.json"
                or launch.get("command") != build_command(manifest, signature, attempt)
                or launch.get("batch_manifest_sha256") != digest(out / "manifest.json")):
            raise IntegrityError("old attempt launch signature/path mismatch")
        next_number = max(next_number, number + 1)
        exit_path = root / (attempt.name + ".exit.json")
        process_path = root / (attempt.name + ".process.json")
        if not exit_path.exists() and process_path.exists():
            process = read_json(process_path)
            if type(process.get("pid")) is int and _pid_alive(process["pid"]):
                return {**_pending(case, problem, cores), "state": "prior_process_may_still_be_running",
                    "attempt_dir": str(attempt), "prior_attempts": prior, "attempt_count": len(launches),
                    "source_status": "launch_verified", "note": "no concurrent duplicate attempt launched"}
        try:
            result = verify_summary(attempt, signature)
            if exit_path.exists() and read_json(exit_path).get("returncode") != 0:
                raise ValueError("child exit status was nonzero")
            row = _success(case, problem, cores, attempt, result, True)
        except (OSError, ValueError, KeyError, TypeError, EOFError) as error:
            row = {"attempt_dir": str(attempt), "outcome": "failed", "error": str(error), **_ledger_hint(attempt)}
        prior.append(row)
    reusable = [r for r in prior if r["outcome"] == "completed_feasible"]
    if reusable:
        row = {**min(reusable, key=lambda r: (r["makespan"], r["added_copy_bytes"])),
               "prior_attempts": prior, "attempt_count": len(launches)}
        atomic_json(root / "slot.json", row)
        return row
    _assert_frozen(manifest, case)
    attempt = root / f"attempt_{next_number:04d}"
    if attempt.exists() or attempt.is_symlink():
        raise IntegrityError("fresh attempt path already exists")
    command = build_command(manifest, signature, attempt)
    launch = {"schema_version": 1, "signature": signature, "signature_sha256": object_digest(signature),
        "command": command, "attempt_number": next_number, "attempt_dir": str(attempt),
        "created_unix": time.time(), "batch_manifest_sha256": digest(out / "manifest.json")}
    atomic_json(root / (attempt.name + ".launch.json"), launch)
    stdout_path, stderr_path = root / (attempt.name + ".stdout.log"), root / (attempt.name + ".stderr.log")
    start, returncode, execution_error = time.monotonic(), None, None
    try:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
            if runner is None:
                process = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=env)
                atomic_json(root / (attempt.name + ".process.json"), {"pid": process.pid, "started_unix": time.time(), "command": command})
                returncode = process.wait()
            else:
                returncode = runner(command, stdout=stdout, stderr=stderr, env=env, check=False).returncode
    except Exception:
        execution_error = traceback.format_exc()
    atomic_json(root / (attempt.name + ".exit.json"), {"returncode": returncode,
        "elapsed_seconds": time.monotonic() - start, "execution_error": execution_error})
    _assert_frozen(manifest, case)
    try:
        if returncode != 0:
            raise ValueError(f"child exit {returncode}: {execution_error or 'see stderr log'}")
        result = verify_summary(attempt, signature)
        row = _success(case, problem, cores, attempt, result, False)
    except (OSError, ValueError, KeyError, TypeError, EOFError) as error:
        row = {**_pending(case, problem, cores), "outcome": "failed", "state": "child_or_evidence_failure",
               "attempt_dir": str(attempt), "error": str(error), **_ledger_hint(attempt)}
    row.update(prior_attempts=prior, attempt_count=next_number, returncode=returncode,
               stdout_path=str(stdout_path), stderr_path=str(stderr_path))
    atomic_json(root / "slot.json", row)
    return row


def aggregate(rows, manifest, *, fatal_errors=None, source_unchanged=None, final=False):
    counts = Counter(r["outcome"] for r in rows)
    return {"schema_version": 1, "batch_protocol": manifest["batch_protocol"],
        "planned_slots": len(rows), "reported_slots": len(rows) - counts["pending"],
        "completed_feasible": counts["completed_feasible"], "failed": counts["failed"], "pending": counts["pending"],
        "completed_count": counts["completed_feasible"], "feasible_count": sum(bool(r.get("feasible")) for r in rows),
        "requires_review_count": sum(bool(r.get("requires_review")) for r in rows),
        "reused_count": sum(bool(r.get("reused")) for r in rows),
        "all_slots_feasible": bool(rows) and all(r.get("feasible") for r in rows),
        "all_searches_completed": bool(rows) and counts["completed_feasible"] == len(rows),
        "source_hashes_unchanged": source_unchanged,
        "selected_attempt_logical_calls": sum(r.get("logical_calls") or 0 for r in rows),
        "failed_attempt_reported_calls_unverified": sum(r.get("reported_logical_calls") or 0 for r in rows if r["outcome"] == "failed"),
        "slots_with_unknown_current_call_count": sum(r["outcome"] != "completed_feasible" and r.get("reported_logical_calls") is None for r in rows),
        "fatal_errors": fatal_errors or [], "final": final, "slots": rows,
        "no_partial_performance_mean_computed": True, "budget_applies_per_attempt": True,
        "scope": manifest["scope"], "initial_scope": manifest["initial_scope"], "walltime_scope": manifest["walltime_scope"]}


def run_batch(settings, out, *, resume=False, runner=None, solve_entry=None):
    out = _outside_official(out)
    entry = Path(solve_entry or HERE / "solve.py").resolve()
    expected = make_manifest(settings, entry)
    if settings.get("evaluation_dir"):
        evaluation_dir = _outside_official(settings["evaluation_dir"])
        if evaluation_dir == out or out in evaluation_dir.parents or evaluation_dir in out.parents:
            raise ValueError("shared evaluation directory must be separate from batch artifacts")
    if out.exists() and not out.is_dir():
        raise ValueError("batch run path is not a directory")
    out.mkdir(parents=True, exist_ok=True)
    with (out / ".batch.lock").open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another process owns this batch run directory")
        manifest_path = out / "manifest.json"
        if manifest_path.exists():
            if not resume or read_json(manifest_path) != expected:
                raise IntegrityError("batch source/input/config/settings mismatch or missing --resume; use a new run directory")
        else:
            if any(p.name != ".batch.lock" for p in out.iterdir()):
                raise IntegrityError("nonempty batch directory lacks a matching manifest")
            atomic_json(manifest_path, expected)
        manifest = expected
        lock_file.seek(0); lock_file.truncate(); lock_file.write(str(os.getpid())); lock_file.flush()
        identities = [(f"case_{c:03d}", p, n) for c in settings["cases"] for p in settings["problems"] for n in settings["cores"]]
        rows = {_key(*ident): _pending(*ident) for ident in identities}
        lock, stop = threading.Lock(), threading.Event()
        fatal = []
        def publish(row=None):
            with lock:
                if row: rows[row["slot"]] = row
                value = aggregate([rows[_key(*i)] for i in identities], manifest, fatal_errors=list(fatal))
                atomic_json(out / "progress.json", value)
        publish()
        def graph_job(number):
            case = f"case_{number:03d}"
            for problem in settings["problems"]:
                for cores in settings["cores"]:
                    if stop.is_set(): return
                    try:
                        row = run_slot(manifest, case, problem, cores, out, resume=resume, runner=runner)
                    except IntegrityError as error:
                        with lock: fatal.append(str(error))
                        stop.set()
                        row = {**_pending(case, problem, cores), "outcome": "failed", "state": "integrity_failure", "error": str(error)}
                    except Exception as error:
                        row = {**_pending(case, problem, cores), "outcome": "failed", "state": "slot_exception",
                            "error": str(error), "traceback": traceback.format_exc()}
                    publish(row)
        with ThreadPoolExecutor(max_workers=settings["workers"]) as pool:
            futures = {pool.submit(graph_job, c): c for c in settings["cases"]}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    with lock: fatal.append(traceback.format_exc())
                    stop.set()
        try:
            _assert_frozen(manifest)
            unchanged = True
        except (OSError, ValueError) as error:
            unchanged = False
            fatal.append(str(error))
        final = aggregate([rows[_key(*i)] for i in identities], manifest,
                          fatal_errors=fatal, source_unchanged=unchanged, final=True)
        atomic_json(out / "summary.json", final)
        atomic_json(out / "progress.json", final)
        return final


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cases", default="all")
    p.add_argument("--problems", default="1,2,3")
    p.add_argument("--cores", default="1,2,3,4,5")
    p.add_argument("--data-dir", type=Path, default=DATA)
    p.add_argument("--config", type=Path)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--seed", type=int, default=17)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--evaluation-dir", type=Path)
    p.add_argument("--incumbent-plan", type=Path, help="explicit single-slot only; default never imports history")
    p.add_argument("--wcc-policy", choices=("mixed", "protected", "unrestricted"), default="mixed")
    for stage, cap in CAP_DEFAULTS.items(): p.add_argument(f"--{stage}-cap", type=int, default=cap)
    p.add_argument("--round-width", type=int, default=6)
    p.add_argument("--max-rounds", type=int, default=5)
    p.add_argument("--max-evaluations", type=int, default=90)
    p.add_argument("--timeout", type=float, help="override per-graph 60/180 second rule")
    p.add_argument("--resume", action="store_true")
    return p


def main(argv=None):
    p = parser(); args = p.parse_args(argv)
    try:
        settings = {"cases": parse_numbers(args.cases, 100, allow_all=True),
            "problems": parse_numbers(args.problems, 3), "cores": parse_numbers(args.cores, 5),
            "data_dir": str(args.data_dir.resolve()), "config": str((args.config or args.data_dir / "config.txt").resolve()),
            "workers": args.workers, "seed": args.seed, "caps": {s: getattr(args, s + "_cap") for s in CAP_DEFAULTS},
            "round_width": args.round_width, "max_rounds": args.max_rounds, "max_evaluations": args.max_evaluations,
            "timeout": args.timeout, "wcc_policy": args.wcc_policy,
            "evaluation_dir": str(args.evaluation_dir.resolve()) if args.evaluation_dir else None,
            "incumbent_plan": str(args.incumbent_plan.resolve()) if args.incumbent_plan else None}
        result = run_batch(settings, args.run_dir, resume=args.resume)
    except (OSError, ValueError) as error:
        p.error(str(error))
    print(json.dumps({"summary": str(args.run_dir.resolve() / "summary.json"),
        **{k: result[k] for k in ("planned_slots", "reported_slots", "completed_feasible", "failed", "pending", "reused_count", "source_hashes_unchanged")}}, ensure_ascii=False))
    return 0 if (result["all_slots_feasible"] and result["all_searches_completed"]
                 and result["source_hashes_unchanged"] and not result["fatal_errors"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
