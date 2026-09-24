#!/usr/bin/env python3
"""Two fixed cold-cache engineering checks, not a performance-panel experiment."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

sys.dont_write_bytecode = True
RESEARCH = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RESEARCH / "精修求解器"))
import quick_refine
from controller import Solver, source_hashes
from solver.common import atomic_json, digest, read_json, DATA
from solver.graph_ir import GraphIR


def run():
    out = RESEARCH / "实验记录/快速入口实测_v1"
    out.mkdir(exist_ok=False)
    frozen = {**quick_refine.source_hashes(), str(Path(__file__).resolve()): digest(__file__)}
    jobs = []
    for c, p in ((1, 2), (93, 3)):
        case = f"case_{c:03d}"
        plan = RESEARCH / f"当前最佳方案_v3_阶段快照/p{p}/n5/{case}_multicore_res.json"
        provenance = plan.with_suffix(".provenance.json")
        record = read_json(provenance)["evaluation_record"]
        if digest(record["result_path"]) != record["result_sha256"]:
            raise ValueError("incumbent official evidence hash mismatch")
        graph = DATA / (case + ".json")
        folder = out / f"{case}_p{p}_n5"
        command = [sys.executable, "-B", str(RESEARCH / "精修求解器/quick_refine.py"), str(graph),
            "-n", "5", "-p", str(p), "--incumbent-plan", str(plan), "--config", str(DATA / "config.txt"),
            "--run-dir", str(folder), "--max-evaluations", "9", "--max-rounds", "2", "--round-width", "4",
            "--timeout", "30", "--time-budget", "120"]
        jobs.append(dict(case=case, problem=p, graph=str(graph), plan=str(plan), run_dir=str(folder),
            baseline_makespan=record["metrics"]["makespan"], command=command))
        for file in (graph, DATA / "config.txt", plan, provenance): frozen[str(file)] = digest(file)
    manifest = dict(scope="two fixed small engineering cases; independent empty per-case cache; not all-graph timing or efficacy evidence",
        created_at=datetime.now().astimezone().isoformat(), authorized_call_cap=18, jobs=jobs, source_and_input_sha256=frozen)
    atomic_json(out / "manifest.json", manifest)
    results = []
    started = time.perf_counter()
    for job in jobs:
        if any(digest(p) != h for p, h in frozen.items()): raise ValueError("frozen source/input changed")
        name = f"{job['case']}_p{job['problem']}"
        atomic_json(out / (name + ".pending.json"), job)
        begin = time.perf_counter()
        with (out / (name + ".stdout.log")).open("w") as stdout, (out / (name + ".stderr.log")).open("w") as stderr:
            process = subprocess.run(job["command"], stdout=stdout, stderr=stderr, start_new_session=True)
        summary = read_json(Path(job["run_dir"]) / "summary.json")
        auditor = SimpleNamespace(manifest=summary, ir=GraphIR.from_path(job["graph"]),
            graph_path=Path(job["graph"]), num_cores=5, problem=job["problem"])
        for trial in summary["evaluations"]:
            if trial.get("record", {}).get("status") == "success":
                Solver.verify_success(auditor, trial["record"], read_json(trial["plan_path"]), trial["plan_sha256"])
        row = dict(case=job["case"], problem=job["problem"], exit_code=process.returncode,
            elapsed_including_cli_seconds=time.perf_counter()-begin, baseline_makespan=job["baseline_makespan"],
            **{k:summary.get(k) for k in ("logical_calls", "cache_hits", "status_counts", "profile_completed", "stop_reason", "requires_review", "best_official_verified")})
        row["final_makespan"] = summary["best"]["record"]["metrics"]["makespan"] if summary.get("best") else None
        row["summary_path"], row["summary_sha256"] = str(Path(job["run_dir"]) / "summary.json"), digest(Path(job["run_dir"]) / "summary.json")
        row["original_incumbent_reproduced"] = summary["evaluations"][0].get("record", {}).get("metrics", {}).get("makespan") == job["baseline_makespan"]
        results.append(row)
        atomic_json(out / "progress.json", dict(results=results))
        print(json.dumps(row, ensure_ascii=False), flush=True)
    result = dict(scope=manifest["scope"], elapsed_including_cli_seconds=time.perf_counter()-started, results=results,
        logical_calls=sum(r["logical_calls"] for r in results), cache_hits=sum(r["cache_hits"] for r in results),
        source_and_input_hashes_verified=all(digest(p)==h for p,h in frozen.items()),
        all_checks_passed=all(r["exit_code"]==0 and r["profile_completed"] and r["best_official_verified"] and not r["requires_review"] and r["original_incumbent_reproduced"] for r in results))
    atomic_json(out / "summary.json", result)
    return result


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["all_checks_passed"] and result["source_and_input_hashes_verified"] else 1)
