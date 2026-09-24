#!/usr/bin/env python3
"""One-shot continuation of the current frozen experiment, in three CPU lanes.

Each lane waits for its already-running main batch, then runs its declared
comparators. This is a finite experiment queue, never a recurring automation.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
from solver.common import atomic_json, read_json, digest

ROOT = RESEARCH / "advanced_solver/runs/formal_v2"
BATCH = RESEARCH / "advanced_solver/batch.py"


def definitions(lane):
    if lane == "a":
        return "full_p1_seed17", 3, [
            ("component_p1_seed17", "1", "2,3,4,5", "component", 17, (12, 0, 0, 0), 12, None),
            ("singlecore_diagnostics_seed17", "1,2,3", "1", "component", 17, (1, 0, 0, 0), 1, 900),
        ]
    if lane == "b":
        return "full_p2_seed17", 3, [
            ("component_p2_seed17", "2", "2,3,4,5", "component", 17, (24, 0, 0, 0), 24, None),
            ("operation_p2_n5_seed17", "2", "5", "operation", 17, (4, 20, 0, 0), 24, None),
            ("full_p2_n5_seed29", "2", "5", "full", 29, (4, 8, 12, 0), 24, None),
            ("full_p2_n5_seed43", "2", "5", "full", 43, (4, 8, 12, 0), 24, None),
        ]
    return "full_p3_seed17", 2, [
        ("component_p3_n5_seed17", "3", "5", "component", 17, (24, 0, 0, 0), 24, None),
        ("operation_p3_n5_seed17", "3", "5", "operation", 17, (4, 20, 0, 0), 24, None),
        ("trace_p3_n5_seed17", "3", "5", "trace", 17, (4, 8, 12, 0), 24, None),
        ("full_p3_n5_seed29", "3", "5", "full", 29, (4, 8, 8, 4), 24, None),
        ("full_p3_n5_seed43", "3", "5", "full", 43, (4, 8, 8, 4), 24, None),
    ]


def commands(lane):
    initial, workers, definitions_ = definitions(lane)
    values = []
    for name, problems, cores, profile, seed, caps, cap, timeout in definitions_:
        command = [sys.executable, "-B", str(BATCH), "--cases", "all", "--problems", problems,
                   "--cores", cores, "--profile", profile, "--seed", str(seed),
                   "--max-evaluations", str(cap), "--max-rounds", "3", "--workers", str(workers),
                   "--evaluation-dir", str(ROOT / "evaluations"), "--run-dir", str(ROOT / name), "--resume"]
        for stage, n in zip(("component", "operation", "trace", "cache"), caps):
            command += [f"--{stage}-cap", str(n)]
        if timeout:
            command += ["--timeout", str(timeout)]
        values.append({"name": name, "command": command})
    return initial, values


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("lane", choices=("a", "b", "c"))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    initial, jobs = commands(args.lane)
    manifest = {"lane": args.lane, "wait_for": initial, "jobs": jobs,
                "driver_sha256": digest(__file__), "batch_sha256": digest(BATCH),
                "singlecore_note": "N=1 diagnostic cap1 and timeout900; not the formal 2..5-core method budget",
                "scope": "finite declared follow-up experiment queue; shared caches charged logically"}
    if args.dry_run:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return
    queue_dir = ROOT / "queues"
    queue_dir.mkdir(exist_ok=True)
    path = queue_dir / (args.lane + ".manifest.json")
    if path.exists() and read_json(path) != manifest:
        raise ValueError("queue source/settings changed")
    atomic_json(path, manifest)
    child = None
    def stop(signum, frame):
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    initial_path = ROOT / initial / "summary.json"
    print("Waiting for", initial, flush=True)
    while not initial_path.exists():
        time.sleep(5)
    prior = read_json(initial_path)
    if not prior["all_slots_feasible"] or not prior["all_searches_completed"] or not prior["source_hashes_unchanged"]:
        raise RuntimeError("Main batch requires review before automatic comparison continuation")
    outcomes = []
    for job in jobs:
        if digest(BATCH) != manifest["batch_sha256"] or digest(__file__) != manifest["driver_sha256"]:
            raise RuntimeError("queue source changed")
        print("Starting", job["name"], flush=True)
        started = time.time()
        with (ROOT / (job["name"] + ".queue.log")).open("ab") as log:
            child = subprocess.Popen(job["command"], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            atomic_json(queue_dir / (args.lane + ".active.json"), {"job": job["name"], "child_pid": child.pid,
                        "driver_pid": os.getpid(), "started_unix": started})
            code = child.wait()
        outcomes.append({"job": job["name"], "exit_code": code, "elapsed_seconds": time.time() - started})
        atomic_json(queue_dir / (args.lane + ".outcomes.json"), outcomes)
        print("Completed", job["name"], "exit", code, flush=True)
        if code:
            raise RuntimeError("Comparison batch requires review; remaining queue stopped")
    atomic_json(queue_dir / (args.lane + ".done.json"), {"completed": True, "outcomes": outcomes})


if __name__ == "__main__":
    main()
