#!/usr/bin/env python3
"""Finite v3 experiment lane; starts only after the corresponding v2 queue."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

RESEARCH = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(RESEARCH), str(RESEARCH / "精修求解器")]
from solver.common import atomic_json, read_json, digest

V2 = RESEARCH / "advanced_solver/runs/formal_v2"
V3 = RESEARCH / "精修求解器/runs/formal_v3"
BATCH = RESEARCH / "精修求解器/batch.py"
PROTOCOL = RESEARCH / "强化实验协议_v3.md"


def jobs(lane):
    problem, workers = {"a": (1, 3), "b": (2, 3), "c": (3, 2)}[lane]
    definitions = [(f"full_p{problem}_seed17", "2,3,4,5", 17)]
    if problem != 1:
        definitions += [(f"full_p{problem}_n5_seed{seed}", "5", seed) for seed in (29, 43)]
    values = []
    for name, cores, seed in definitions:
        command = [sys.executable, "-B", str(BATCH), "--cases", "all", "--problems", str(problem),
                   "--cores", cores, "--workers", str(workers), "--seed", str(seed),
                   "--run-dir", str(V3 / name), "--evaluation-dir", str(V2 / "evaluations"),
                   "--component-cap", "6", "--operation-cap", "12", "--selective-cap", "18",
                   "--wcc-cap", "9", "--wcc-policy", "mixed", "--trace-cap", "30", "--cache-cap", "18",
                   "--round-width", "6", "--max-rounds", "5", "--max-evaluations", "90", "--resume"]
        values.append({"name": name, "command": command})
    return values


def main(args):
    values = jobs(args.lane)
    if args.dry_run:
        print(json.dumps(values, ensure_ascii=False, indent=2))
        return
    from controller import source_hashes
    sources = {**source_hashes(), str(BATCH): digest(BATCH), str(PROTOCOL): digest(PROTOCOL),
               str(Path(__file__).resolve()): digest(__file__)}
    queues = V3 / "queues"
    queues.mkdir(parents=True, exist_ok=True)
    manifest_path = queues / (args.lane + ".manifest.json")
    manifest = {"scope": "finite current-task experiment continuation, no recurring scheduler",
                "lane": args.lane, "wait_for": str(V2 / "queues" / (args.lane + ".done.json")),
                "max_wait_hours": args.max_wait_hours, "jobs": values, "source_sha256": sources}
    if manifest_path.exists() and read_json(manifest_path) != manifest:
        raise ValueError("v3 queue source/settings changed; use a separately declared version")
    atomic_json(manifest_path, manifest)
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
    started = time.monotonic()
    dependency = Path(manifest["wait_for"])
    print("Waiting for", dependency, flush=True)
    while not dependency.exists():
        problem = {"a": 1, "b": 2, "c": 3}[args.lane]
        main_summary = V2 / f"full_p{problem}_seed17/summary.json"
        if main_summary.exists() and not all(read_json(main_summary).get(k) for k in
                ("all_slots_feasible", "all_searches_completed", "source_hashes_unchanged")):
            atomic_json(queues / (args.lane + ".needs_review.json"),
                        {"reason": "v2 main batch failed or incomplete", "path": str(main_summary)})
            raise RuntimeError("v2 main batch needs review before v3 continuation")
        failures = V2 / "queues" / (args.lane + ".outcomes.json")
        if failures.exists() and any(r["exit_code"] for r in read_json(failures)):
            atomic_json(queues / (args.lane + ".needs_review.json"), {"reason": "v2 predecessor failed", "path": str(failures)})
            raise RuntimeError("v2 failure needs review before v3 continuation")
        if time.monotonic() - started > args.max_wait_hours * 3600:
            raise TimeoutError("finite dependency waiting time expired")
        time.sleep(15)
    prior = read_json(dependency)
    if not prior.get("completed") or any(r["exit_code"] for r in prior["outcomes"]):
        raise RuntimeError("v2 dependency is not complete and successful")
    atomic_json(queues / (args.lane + ".dependency.json"), {"path": str(dependency), "sha256": digest(dependency)})
    outcomes = []
    for job in values:
        if any(digest(p) != h for p, h in sources.items()):
            raise RuntimeError("frozen v3 source changed before starting a batch")
        print("Starting", job["name"], flush=True)
        begin = time.time()
        with (V3 / (job["name"] + ".queue.log")).open("ab") as log:
            child = subprocess.Popen(job["command"], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            atomic_json(queues / (args.lane + ".active.json"), {"job": job["name"], "child_pid": child.pid,
                        "driver_pid": os.getpid(), "started_unix": begin})
            code = child.wait()
        outcome = {"job": job["name"], "exit_code": code, "elapsed_seconds": time.time() - begin}
        if code == 0:
            summary = read_json(V3 / job["name"] / "summary.json")
            if not all(summary.get(k) for k in ("all_slots_feasible", "all_searches_completed", "source_hashes_unchanged")):
                outcome["exit_code"] = code = 2
                outcome["reason"] = "batch process exited zero but incomplete/unverified matrix"
        outcomes.append(outcome)
        atomic_json(queues / (args.lane + ".outcomes.json"), outcomes)
        print("Completed", job["name"], "exit", code, flush=True)
        if code:
            atomic_json(queues / (args.lane + ".needs_review.json"), outcome)
            raise RuntimeError("v3 batch needs review; remaining jobs stopped")
    atomic_json(queues / (args.lane + ".done.json"), {"completed": True, "outcomes": outcomes})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", choices=("a", "b", "c"))
    parser.add_argument("--max-wait-hours", type=float, default=48)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 0 < args.max_wait_hours <= 72:
        parser.error("waiting must be finite, positive and at most 72 hours")
    main(args)
