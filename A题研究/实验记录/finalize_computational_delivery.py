#!/usr/bin/env python3
"""Finite, fail-closed computational delivery chain; never a recurring task.

All output directories must be new. No automatic retries, no partial delivery,
no paper generation, and no upload. Importing this module performs no work.
"""
import argparse
import ast
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
EXPECTED_KEYS = {(f"case_{i:03d}", p, n) for i in range(1, 101) for p in (1, 2, 3) for n in range(1, 6)}
TOOLS = ("collect_best_known.py", "summarize_formal.py", "summarize_v3.py", "summarize_portfolio.py", "make_delivery.py", "verify_delivery.py")


class StopChain(RuntimeError):
    def __init__(self, state, message):
        super().__init__(message)
        self.state = state


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read(path):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("duplicate JSON key: " + k)
            result[k] = v
        return result
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError("nonfinite JSON: " + x)))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def require(condition, message):
    if not condition:
        raise StopChain("needs_review", message)


def path_entry(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha(path)}


def graph_scope(settings):
    return {(f"case_{i:03d}", p, n) for i in settings["cases"] for p in settings["problems"] for n in settings["cores"]}


def fixed_specs():
    v2 = {f"full_p{p}_seed17": (p, [2, 3, 4, 5], 17) for p in (1, 2, 3)}
    v2.update({f"component_p{p}_seed17": (p, [2, 3, 4, 5], 17) for p in (1, 2)})
    v2["singlecore_diagnostics_seed17"] = ([1, 2, 3], [1], 17)
    for p in (2, 3):
        v2[f"operation_p{p}_n5_seed17"] = (p, [5], 17)
        for seed in (29, 43): v2[f"full_p{p}_n5_seed{seed}"] = (p, [5], seed)
    for method in ("component", "trace"): v2[f"{method}_p3_n5_seed17"] = (3, [5], 17)
    v3 = {f"full_p{p}_seed17": (p, [2, 3, 4, 5], 17) for p in (1, 2, 3)}
    for p in (2, 3):
        for seed in (29, 43): v3[f"full_p{p}_n5_seed{seed}"] = (p, [5], seed)
    return {"v2": v2, "v3": v3}


def default_config(research=RESEARCH, *, run_dir=None, pre_portfolio=None, portfolio=None, package=None,
                   max_wait_hours=60, max_stage_hours=24, interval=15, stable_seconds=2):
    research = Path(research).resolve()
    logs = research / "实验记录"
    return {"research": str(research), "run_dir": str(Path(run_dir).resolve() if run_dir else logs / "完整计算交付收尾_v3"),
            "v2_root": str(research / "advanced_solver/runs/formal_v2"),
            "v3_root": str(research / "精修求解器/runs/formal_v3"),
            "four_cells": str(logs / "正式P3四格_v2"),
            "snapshot_manifest": str(research / "当前最佳方案_v3_阶段快照/manifest.json"),
            "pre_portfolio": str(Path(pre_portfolio).resolve() if pre_portfolio else research / "当前最佳方案_继承前完整v3"),
            "inheritance": str(logs / "完整v3低核继承"),
            "portfolio": str(Path(portfolio).resolve() if portfolio else research / "当前最佳方案_完整v3"),
            "package": str(Path(package).resolve() if package else research / "计算交付_v3_完整"),
            "max_wait_hours": max_wait_hours, "max_stage_hours": max_stage_hours, "interval": interval,
            "stable_seconds": stable_seconds, "inheritance_cap": 1200,
            "python": str(Path(sys.executable).resolve()), "python_version": sys.version}


def local_import_closure(initial, research):
    """Conservatively resolve local AST imports only; no source is executed here."""
    research = Path(research)
    roots = [research / "实验记录", research / "精修求解器", research, research / "solver",
             research / "advanced_solver", research / "探索", research.parent / "选题分析/A题附件/code"]
    seen, queue = set(), [Path(p).resolve() for p in initial]
    while queue:
        p = queue.pop()
        if p in seen: continue
        if not p.is_file(): raise ValueError("dependency not present/frozen: " + str(p))
        seen.add(p)
        if p.suffix != ".py": continue
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import): modules = [x.name for x in node.names]
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                if node.module: modules = [node.module] + [node.module + "." + x.name for x in node.names]
            for module in modules:
                parts = module.split(".")
                for root in [p.parent] + roots:
                    base = root.joinpath(*parts)
                    candidates = [base.with_suffix(".py"), base / "__init__.py"]
                    for candidate in candidates:
                        if candidate.is_file():
                            queue.append(candidate.resolve())
                            for length in range(1, len(parts)):
                                init = root.joinpath(*parts[:length]) / "__init__.py"
                                if init.is_file(): queue.append(init.resolve())
    return seen


def source_inventory(config):
    research = Path(config["research"])
    controller_path = research / "精修求解器/controller.py"
    sys.path[:0] = [str(controller_path.parent), str(research)]
    spec = importlib.util.spec_from_file_location("_finalizer_controller", controller_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    declared = module.source_hashes()
    files = set(declared)
    files.update(str(research / "实验记录" / name) for name in TOOLS)
    files.update(str(research / name) for name in ("精修求解器/batch.py", "精修求解器/core_inheritance.py", "advanced_solver/batch.py",
                "正式实验协议_v2.md", "强化实验协议_v3.md", "实验记录/run_formal_followups.py", "实验记录/run_v3_followups.py", "实验记录/formal_four_cells.py"))
    files.add(str(Path(__file__).resolve()))
    closure = local_import_closure(files, research)
    hashes = {str(p): sha(p) for p in sorted(closure)}
    require(all(hashes.get(p) == h for p, h in declared.items()), "controller source changed during freeze")
    return {"controller_declared": declared, "sources": hashes}


def freeze(config, *, source_provider=source_inventory, sleep=time.sleep):
    for name in ("max_wait_hours", "max_stage_hours", "interval", "stable_seconds"):
        value = config[name]
        require(type(value) in (int, float) and math.isfinite(value) and value > 0, "invalid finite bound: " + name)
    require(config["max_wait_hours"] <= 72 and config["max_stage_hours"] <= 48 and config["interval"] <= 60 and config["stable_seconds"] <= 60, "bound exceeds permitted maximum")
    snapshot = read(config["snapshot_manifest"])
    benchmarks = list(dict.fromkeys(str(Path(p).resolve()) for p in snapshot["benchmark_runs"]))
    exploratory = list(dict.fromkeys(str(Path(p).resolve()) for p in snapshot["exploratory_runs"] + [config["four_cells"]]))
    require(benchmarks and exploratory, "empty collection roots")
    for p in benchmarks + exploratory: require(Path(p).is_dir(), "declared source root missing: " + p)
    outputs = [Path(config[k]).resolve() for k in ("run_dir", "pre_portfolio", "inheritance", "portfolio", "package")]
    protected = [Path(p).resolve() for p in benchmarks + exploratory]
    protected += [Path(config["research"]).parent / "选题分析/A题附件"]
    protected += [Path(config["research"]) / k for k in ("solver", "advanced_solver", "精修求解器", "探索")]
    for i, out in enumerate(outputs):
        require(not out.exists() and not out.is_symlink(), "strictly fresh output required: " + str(out))
        for other in outputs[:i] + protected:
            require(out != other and out not in other.parents and other not in out.parents, "output overlaps input/other output: " + str(out))
    source = source_provider(config)
    immutable = {str(Path(config["snapshot_manifest"]).resolve()): sha(config["snapshot_manifest"])}
    for version in ("v2", "v3"):
        for lane in "abc":
            p = Path(config[version + "_root"]) / "queues" / (lane + ".manifest.json")
            immutable[str(p)] = sha(p)
            for source_path, expected in read(p).get("source_sha256", {}).items():
                require(sha(source_path) == expected, "predecessor frozen source changed: " + source_path)
    four_launch = Path(config["four_cells"]) / "launch.json"
    immutable[str(four_launch)] = sha(four_launch)
    data = Path(config["research"]).parent / "选题分析/A题附件/data"
    # The authoritative data directory is bound by an already-frozen v2 main manifest.
    main = read(Path(config["v2_root"]) / "full_p1_seed17/manifest.json")
    require(main.get("python") == sys.version and Path(main.get("python_executable", "")).resolve() == Path(sys.executable).resolve(),
            "use the same bundled Python runtime as the frozen v2 experiment")
    data = Path(main["settings"]["data_dir"])
    require(sha(main["settings"]["config"]) == main["config_sha256"], "official configuration changed before launch")
    immutable[main["settings"]["config"]] = main["config_sha256"]
    for source_path, expected in main.get("source_sha256", {}).items():
        require(sha(source_path) == expected, "v2 original source changed before launch: " + source_path)
    for case, expected in main["graphs_sha256"].items():
        p = data / (case + ".json")
        require(sha(p) == expected, "official graph mismatch at launch: " + case)
        immutable[str(p)] = expected
    require(len(main["graphs_sha256"]) == 100, "must freeze 100 original graphs")
    immutable[str(Path(config["v2_root"]) / "full_p1_seed17/manifest.json")] = sha(Path(config["v2_root"]) / "full_p1_seed17/manifest.json")
    launch = {"schema_version": 1, "created_at": now(), "config": config, "source_sha256": source["sources"],
              "controller_declared_source_sha256": source["controller_declared"], "immutable_input_sha256": immutable,
              "benchmark_runs": benchmarks, "exploratory_runs": exploratory,
              "expected_v2_slots": 3100, "expected_v3_slots": 1600, "expected_portfolio_slots": 1500,
              "scope": "finite current experiment chain; no automatic retry; best-known portfolio is not a same-budget algorithm result",
              "walltime_scope": "shared exact cache and concurrent histories are not cold-cache speed comparisons"}
    sleep(config["stable_seconds"])
    check_frozen(launch)
    return launch


def check_frozen(launch, *, include_inputs=True):
    values = dict(launch["source_sha256"])
    if include_inputs: values.update(launch["immutable_input_sha256"])
    for path, expected in values.items():
        if not Path(path).is_file() or sha(path) != expected:
            raise StopChain("source_changed", "frozen source/input changed: " + path)


def probe_dependencies(launch):
    c = launch["config"]
    pending, receipts, counts, historical_calls = [], [], {}, {}
    specs = fixed_specs()
    for version in ("v2", "v3"):
        root = Path(c[version + "_root"])
        queue_names = set()
        for lane in "abc":
            q = root / "queues"
            if (q / (lane + ".needs_review.json")).exists():
                raise StopChain("needs_review", "queue needs review: " + str(q / (lane + ".needs_review.json")))
            m = read(q / (lane + ".manifest.json"))
            jobs = [x["name"] for x in m["jobs"]]
            queue_names.update(jobs)
            if version == "v2": queue_names.add(m["wait_for"])
            outcomes = q / (lane + ".outcomes.json")
            if outcomes.exists(): require(all(x.get("exit_code") == 0 for x in read(outcomes)), "queue failed: " + str(outcomes))
            done = q / (lane + ".done.json")
            if not done.exists(): pending.append(str(done))
            else:
                value = read(done)
                require(value.get("completed") is True and [x.get("job") for x in value.get("outcomes", [])] == jobs
                        and all(x.get("exit_code") == 0 for x in value["outcomes"]), "queue done is incomplete/failed: " + str(done))
                receipts.append(path_entry(done))
        require(queue_names == set(specs[version]), version + " queue scope differs from declared final experiment")
        complete = 0
        selected_calls = 0
        unknown_calls = 0
        for name, (problem, cores, seed) in specs[version].items():
            folder = root / name
            summary = folder / "summary.json"
            progress = folder / "progress.json"
            current = read(summary) if summary.exists() else (read(progress) if progress.exists() else {})
            require(not current.get("fatal_errors") and not current.get("requires_review_count") and not current.get("failed"), "batch failure: " + str(folder))
            for row in current.get("slots", []):
                require(not row.get("requires_review") and row.get("outcome") in (None, "pending", "completed_feasible", "success"), "batch slot failed/requires review: " + str(folder) + " " + str(row.get("slot")))
            if not summary.exists(): pending.append(str(summary)); continue
            require(all(current.get(k) is True for k in ("all_slots_feasible", "all_searches_completed", "source_hashes_unchanged")), "incomplete/unverified final batch: " + str(summary))
            manifest = read(folder / "manifest.json")
            settings = manifest["settings"]
            problems = problem if isinstance(problem, list) else [problem]
            require(settings["cases"] == list(range(1, 101)) and settings["problems"] == problems and settings["cores"] == cores and settings["seed"] == seed, "wrong batch scope: " + name)
            expected = graph_scope(settings)
            slots = current.get("slots", [])
            keys = {(r["case"], r["problem"], r["num_cores"]) for r in slots}
            require(len(slots) == len(expected) and keys == expected and current["planned_slots"] == current["reported_slots"] == len(expected), "batch matrix mismatch: " + name)
            require(all(r.get("feasible") is True and r.get("search_completed") is True for r in slots), "batch boolean hides incomplete row: " + name)
            complete += len(slots)
            for row in slots:
                calls = row.get("logical_calls") if version == "v3" else row.get("evaluated_count")
                if type(calls) is int: selected_calls += calls
                else: unknown_calls += 1
            receipts.extend([path_entry(summary), path_entry(folder / "manifest.json")])
        counts[version + "_complete_slots"] = complete
        historical_calls[version] = {"selected_successful_attempt_logical_calls": selected_calls,
                                     "unknown_slot_call_counts": unknown_calls,
                                     "scope": "already completed experiments; excludes prior failed attempts, not new finalizer calls"}
    four = Path(c["four_cells"]) / "summary.json"
    controller_error = four.with_name("controller_error.json")
    require(not controller_error.exists(), "formal four-cell controller failed: " + str(controller_error))
    if not four.exists():
        pending.append(str(four))
        progress = four.with_name("progress.json")
        if progress.exists():
            statuses = read(progress).get("status_counts", {})
            require(all(k in ("success", "reserved") or not v for k, v in statuses.items()), "formal four-cell call failure: " + str(progress))
    else:
        s = read(four)
        require(s.get("all_four_cells_complete") is True and s.get("four_cell_complete_count") == 400 and s.get("source_hashes_unchanged") is True,
                "formal four-cell summary incomplete/changed")
        require(not s.get("pair_controller_failures") and not s.get("unresolved_calls"), "formal four-cell unresolved/controller failure")
        pairs = s.get("pairs", [])
        require(len(pairs) == 400 and {(r["case"], r["num_cores"]) for r in pairs} == {(f"case_{i:03d}", n) for i in range(1, 101) for n in (2, 3, 4, 5)}, "four-cell matrix mismatch")
        require(all(r.get("four_cell_complete") is True for r in pairs), "four-cell summary hides missing row")
        # The atomic summary precedes CSV creation; finished progress is written
        # only after the CSV stream closes. Do not mistake that short window for
        # failure or complete output publication.
        final_progress = four.with_name("progress.json")
        progress_value = read(final_progress) if final_progress.is_file() else {}
        if progress_value.get("phase") != "finished":
            pending.append(str(final_progress) + "#phase=finished")
        else:
            csv_path = four.with_name("four_cells.csv")
            require(csv_path.is_file(), "finished four-cell run lacks CSV")
            receipts.extend([path_entry(four), path_entry(final_progress), path_entry(csv_path)])
            counts["four_cell_pairs"] = 400
            historical_calls["formal_four_cells"] = {"logical_cross_calls": s.get("logical_cross_calls"),
                                                     "cache_hits": s.get("cache_hits"), "scope": "already completed cross calls"}
    return {"ready": not pending, "pending": pending, "receipts": receipts, "historical_calls": historical_calls, **counts}


def inheritance_call_state(directory):
    """Conservative crash accounting; pending is not proof a worker ran."""
    root = Path(directory)
    if not root.exists(): return {"new_inheritance_logical_calls": 0, "inheritance_started": False}
    pending = list(root.glob("case_*/p*_n*/pending.json"))
    paths = list(root.glob("case_*/p*_n*/result.json"))
    rows = [read(p) for p in paths]
    statuses = dict(Counter(r.get("record", {}).get("status", "missing") for r in rows))
    return {"new_inheritance_logical_calls": len(pending), "inheritance_started": True,
            "inheritance_reserved_plan_count": len(pending), "inheritance_completed_records": len(rows),
            "inheritance_unresolved_reserved_calls": max(0, len(pending) - len(rows)),
            "inheritance_status_counts": statuses,
            "inheritance_cache_hits": sum(bool(r.get("record", {}).get("cache_hit")) for r in rows),
            "inheritance_accepted": sum(bool(r.get("accepted")) for r in rows),
            "inheritance_count_scope": "pending plans counted conservatively as calls; an unresolved reservation does not prove an official worker ran"}


def inventory_records(launch):
    paths = set()
    for root in launch["benchmark_runs"]:
        paths.update(p for p in (Path(root) / "results").glob("case_*/*_p*_n*_seed*.json") if not p.name.endswith(".plan.json"))
    for root in launch["exploratory_runs"]: paths.update(Path(root).rglob("record.json"))
    return {str(p.resolve()): sha(p) for p in sorted(paths)}


def verify_portfolio(directory):
    directory = Path(directory)
    m = read(directory / "manifest.json")
    require(m.get("selected_count") == 1500 and m.get("expected_full_coverage") == 1500 and m.get("full_coverage") is True and m.get("rejected") == [], "portfolio is incomplete or has rejected evidence: " + str(directory))
    with (directory / "catalog.csv").open(encoding="utf-8-sig", newline="") as f: rows = list(csv.DictReader(f))
    keys = [(r["case"], int(r["problem"]), int(r["num_cores"])) for r in rows]
    require(len(keys) == 1500 and set(keys) == EXPECTED_KEYS, "portfolio has missing/duplicate/out-of-scope slots")
    coverage = dict(Counter(f"p{p}_n{n}" for _, p, n in keys))
    require(m["coverage"] == coverage, "portfolio coverage declaration mismatch")
    for row in rows:
        p = Path(row["plan_path"])
        expected = directory / f"p{row['problem']}" / f"n{row['num_cores']}" / (row["case"] + "_multicore_res.json")
        require(p.resolve() == expected.resolve() and p.is_file() and sha(p) == row["plan_sha256"], "selected catalog plan hash/path mismatch")
    return {"selected_count": len(rows), "coverage": coverage, "rejected_count": 0,
            "manifest": path_entry(directory / "manifest.json"), "catalog": path_entry(directory / "catalog.csv")}


def stop_process(child):
    if child.poll() is not None: return
    try: os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError: return
    try: child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try: os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        child.wait()


def run_process(command, stdout, stderr, heartbeat, timeout, interval):
    started = time.monotonic()
    with Path(stdout).open("wb") as out, Path(stderr).open("wb") as err:
        child = subprocess.Popen(command, stdout=out, stderr=err, start_new_session=True)
        write(Path(stdout).with_name("process.json"), {"pid": child.pid, "parent_pid": os.getpid(), "started_at": now()})
        try:
            while True:
                heartbeat()
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0: raise StopChain("timed_out", "bounded child stage timeout")
                try: return child.wait(timeout=min(interval, remaining))
                except subprocess.TimeoutExpired: pass
        finally:
            stop_process(child)


def run_chain(config, *, runner=run_process, source_provider=source_inventory, sleep=time.sleep,
              monotonic=time.monotonic, probe=probe_dependencies, records=inventory_records):
    # Preflight cannot create/overwrite a directory on failure.
    launch = freeze(config, source_provider=source_provider, sleep=sleep)
    out = Path(config["run_dir"])
    out.mkdir(parents=True, exist_ok=False)
    write(out / "launch.json", launch)
    status = {"schema_version": 1, "state": "waiting", "started_at": now(), "launch": path_entry(out / "launch.json"),
              "stages": [], "paths": {k: config[k] for k in ("run_dir", "pre_portfolio", "inheritance", "portfolio", "package")},
              "calls": {"new_inheritance_logical_calls": 0, "other_new_official_calls": 0}, "completed": False}
    def publish():
        status["updated_at"] = now(); write(out / "status.json", status)
    def heartbeat():
        check_frozen(launch, include_inputs=False)
    def check_receipts():
        for item in status.get("dependencies", {}).get("receipts", []):
            require(Path(item["path"]).is_file() and sha(item["path"]) == item["sha256"], "completed dependency changed: " + item["path"])
    def stage(name, command, validate, *, require_new=None):
        check_frozen(launch); check_receipts()
        if require_new: require(not Path(require_new).exists(), "stage output no longer fresh: " + str(require_new))
        folder = out / "stages" / f"{len(status['stages']) + 1:02d}_{name}"
        folder.mkdir(parents=True, exist_ok=False)
        entry = {"stage": name, "state": "pending", "command": command, "started_at": now(),
                 "stdout": str(folder / "stdout.log"), "stderr": str(folder / "stderr.log"),
                 "source_manifest_sha256": sha(out / "launch.json"), "stage_timeout_seconds": config["max_stage_hours"] * 3600}
        status["stages"].append(entry); status.update(state="running", current_stage=name)
        write(folder / "pending.json", entry); publish()
        begin = monotonic()
        try:
            code = runner(command, entry["stdout"], entry["stderr"], heartbeat,
                          config["max_stage_hours"] * 3600, config["interval"])
            entry.update(exit_code=code, elapsed_seconds=monotonic() - begin)
            for label in ("stdout", "stderr"):
                p = Path(entry[label])
                if p.is_file(): entry[label + "_sha256"] = sha(p)
            require(code == 0, "stage exited nonzero: " + name + " exit=" + str(code))
            check_frozen(launch); check_receipts()
            entry["validation"] = validate(entry)
            entry.update(state="completed", finished_at=now())
        except BaseException as error:
            entry.update(state=getattr(error, "state", "failed"), error=f"{type(error).__name__}: {error}",
                         elapsed_seconds=monotonic() - begin, finished_at=now())
            raise
        finally:
            write(folder / "result.json", entry); publish()
        return entry["validation"]
    publish()
    try:
        begin = monotonic()
        while True:
            check_frozen(launch)
            status["dependencies"] = probe(launch); publish()
            if status["dependencies"]["ready"]: break
            remaining = config["max_wait_hours"] * 3600 - (monotonic() - begin)
            if remaining <= 0: raise StopChain("timed_out", "finite dependency waiting deadline expired; no stages started")
            sleep(min(config["interval"], remaining))
        write(out / "dependency_receipts.json", status["dependencies"])
        status["calls"]["historical"] = status["dependencies"].get("historical_calls", {})
        py = [config["python"], "-B"]
        scripts = Path(config["research"]) / "实验记录"
        formal_out, v3_out = out / "正式实验汇总_v2", out / "强化实验汇总_v3"
        def audit_summary(path, version):
            s = read(path)
            a, b, expected = ("planned_slots", "verified_complete", 3100) if version == "v2" else ("slot_count", "verified_slots", 1600)
            require(s.get(a) == s.get(b) == expected and s.get("audit_errors") == [], version + " audit incomplete/rejected")
            require(s.get("aggregates") and all(x.get("all100" if version == "v2" else "all_100") is True for x in s["aggregates"]), version + " incomplete aggregate group")
            return {"verified_slots": expected, "summary": path_entry(path)}
        stage("summarize_v2", py + [str(scripts / "summarize_formal.py"), "--root", config["v2_root"], "--out", str(formal_out)], lambda e: audit_summary(formal_out / "summary.json", "v2"), require_new=formal_out)
        stage("summarize_v3", py + [str(scripts / "summarize_v3.py"), "--root", config["v3_root"], "--output", str(v3_out)], lambda e: audit_summary(v3_out / "summary.json", "v3"), require_new=v3_out)
        def collect_args(output):
            args = py + [str(scripts / "collect_best_known.py")]
            for root in launch["benchmark_runs"]: args += ["--benchmark", root]
            for root in launch["exploratory_runs"]: args += ["--exploratory", root]
            return args + ["--output", output]
        def collect_stage(name, output):
            before = records(launch)
            write(out / (name + ".input_records.json"), before)
            def validate(e):
                require(records(launch) == before, "candidate record set changed during " + name)
                return verify_portfolio(output)
            return stage(name, collect_args(output), validate, require_new=output)
        status["coverage_before_inheritance"] = collect_stage("collect_before_inheritance", config["pre_portfolio"])
        initial_manifest_sha = sha(Path(config["pre_portfolio"]) / "manifest.json")
        catalog_sha = sha(Path(config["pre_portfolio"]) / "catalog.csv")
        def inherited(e):
            root = Path(config["inheritance"])
            m, s = read(root / "manifest.json"), read(root / "summary.json")
            require(m.get("catalog_sha256") == catalog_sha and sha(Path(config["pre_portfolio"]) / "catalog.csv") == catalog_sha and sha(Path(config["pre_portfolio"]) / "manifest.json") == initial_manifest_sha, "inheritance catalog changed")
            require(m.get("logical_cap") == 1200 and s.get("sources_unchanged") is True, "inheritance cap/source mismatch")
            rows = s.get("results", [])
            require(type(s.get("logical_calls")) is int and s["logical_calls"] == len(rows) == len(m["proposals"]) <= 1200, "inheritance incomplete call ledger")
            statuses = dict(Counter(r.get("record", {}).get("status", "missing") for r in rows))
            status["calls"].update(new_inheritance_logical_calls=len(rows), inheritance_status_counts=statuses,
                                   inheritance_cache_hits=sum(bool(r.get("record", {}).get("cache_hit")) for r in rows),
                                   inheritance_accepted=sum(bool(r.get("accepted")) for r in rows))
            require(all(r.get("record", {}).get("status") == "success" for r in rows), "inheritance official failure retained; no automatic retry")
            # A successful slower/equal padding is a valid negative result, never invented acceptance.
            return {"logical_calls": len(rows), "status_counts": statuses, "accepted": s["accepted"],
                    "all_padding_times_equal_observed": s["all_padding_times_equal"], "summary": path_entry(root / "summary.json")}
        stage("core_inheritance", py + [str(Path(config["research"]) / "精修求解器/core_inheritance.py"), str(Path(config["pre_portfolio"]) / "catalog.csv"),
              "--run-dir", config["inheritance"], "--evaluation-dir", str(Path(config["v2_root"]) / "evaluations"), "--max-evaluations", "1200"], inherited, require_new=config["inheritance"])
        launch["exploratory_runs"] = launch["exploratory_runs"] + [config["inheritance"]]
        # The original launch stays immutable; explicitly journal the sole authorized scope addition.
        write(out / "post_inheritance_collection_scope.json", {"benchmark_runs": launch["benchmark_runs"], "exploratory_runs": launch["exploratory_runs"], "reason": "include the just-completed, separately budgeted inheritance pass"})
        status["coverage"] = collect_stage("collect_complete_portfolio", config["portfolio"])
        def metrics(e):
            s = read(Path(config["portfolio"]) / "metrics_summary.json")
            require(s.get("selected_count") == 1500 and s.get("full_coverage") is True and len(s.get("groups", [])) == 15 and all(g.get("all_100") is True and g.get("completed") == 100 for g in s["groups"]), "portfolio metrics incomplete")
            return {"summary": path_entry(Path(config["portfolio"]) / "metrics_summary.json"), "complete_groups": 15}
        stage("summarize_portfolio", py + [str(scripts / "summarize_portfolio.py"), config["portfolio"]], metrics)
        def packaged(e):
            r = read(e["stdout"])
            require(r.get("status") == "verified_complete" and r.get("selected_count") == 1500 and r.get("missing_count") == 0, "packer did not return complete verified delivery")
            p = Path(config["package"])
            require(sha(p / "delivery_manifest.json") == r.get("manifest_sha256"), "packed manifest hash mismatch")
            require(sha(p / "verify_delivery.py") == sha(scripts / "verify_delivery.py"), "packaged verifier differs from frozen original")
            return r
        built = stage("make_delivery", py + [str(scripts / "make_delivery.py"), "--portfolio", config["portfolio"], "--output", config["package"]], packaged, require_new=config["package"])
        def verified(e):
            r = read(e["stdout"])
            require(r.get("status") == "verified_complete" and r.get("selected_count") == 1500 and r.get("missing_count") == 0 and r.get("manifest_sha256") == built["manifest_sha256"] and r.get("official_evaluations_performed") == 0, "package verification failed/incomplete")
            return r
        status["delivery"] = stage("verify_delivery", py + [str(Path(config["package"]) / "verify_delivery.py"), config["package"], "--manifest-sha256", built["manifest_sha256"]], verified)
        check_frozen(launch); check_receipts()
        status.update(state="complete", completed=True, source_hashes_unchanged=True, finished_at=now())
    except BaseException as error:
        status.update(state=getattr(error, "state", "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"),
                      completed=False, error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc(), finished_at=now(),
                      automatic_retry=False, evidence_preserved=True)
        try: check_frozen(launch); status["source_hashes_unchanged"] = True
        except BaseException: status["source_hashes_unchanged"] = False
    try:
        status["calls"].update(inheritance_call_state(config["inheritance"]))
    except Exception as error:
        status["calls"].update(new_inheritance_logical_calls=None, inheritance_call_accounting_error=str(error))
        status.update(state="needs_review", completed=False)
    publish()
    return status


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path)
    p.add_argument("--pre-portfolio", type=Path)
    p.add_argument("--portfolio", type=Path)
    p.add_argument("--package", type=Path)
    p.add_argument("--max-wait-hours", type=float, default=60)
    p.add_argument("--max-stage-hours", type=float, default=24)
    p.add_argument("--interval", type=float, default=15)
    p.add_argument("--stable-seconds", type=float, default=2)
    a = p.parse_args(argv)
    config = default_config(**vars(a))
    def stop(signum, frame): raise KeyboardInterrupt("signal " + str(signum))
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    try: result = run_chain(config)
    except Exception as error:
        print(json.dumps({"state": "preflight_failed", "error": f"{type(error).__name__}: {error}", "no_stage_started": True}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({k: result.get(k) for k in ("state", "completed", "paths", "coverage", "calls", "error")}, ensure_ascii=False))
    return 0 if result["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
