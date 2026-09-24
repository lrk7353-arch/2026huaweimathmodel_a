"""All-case operation exploration with saved WCC controls; not a cold-budget comparison."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import json
from pathlib import Path
import platform
import shutil
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "solver"), str(HERE.parent / "探索")]
from common import DATA, OFFICIAL, ROOT, atomic_json, digest, object_digest, read_json
from graph_ir import GraphIR
from evaluator import evaluate
from operation_assign import generate_operation_candidates


def key(record):
    return record["metrics"]["makespan"], record["metrics"].get("data_movement_bytes", {}).get("added_copy_bytes", 0)


def validated_control(path, graph, cores):
    source = read_json(path)
    best = source["best"]
    record = best["record"]
    if (source["problem"] != 2 or source["num_cores"] != cores or record["status"] != "success"
            or record["hashes"]["graph_sha256"] != digest(graph)
            or record["hashes"]["config_sha256"] != digest(DATA / "config.txt")
            or record["hashes"]["official_py_sha256"] != {p.name: digest(p) for p in sorted(OFFICIAL.glob("*.py"))}
            or digest(record["result_path"]) != record["result_sha256"]
            or object_digest(best["plan"]) != record["hashes"]["plan_sha256"]):
        raise ValueError("Control provenance mismatch: " + str(path))
    with gzip.open(record["result_path"], "rt", encoding="utf-8") as stream:
        official = json.load(stream)
    if official["makespan"] != record["metrics"]["makespan"] or official["data_movement_bytes"] != record["metrics"]["data_movement_bytes"]:
        raise ValueError("Control metrics differ from immutable official output")
    return {"name": "saved_wcc_control", "plan": best["plan"], "record": record,
            "source_path": str(path), "source_sha256": digest(path)}


def run_case(graph, args, signature):
    target = args.run_dir / "results" / (graph.stem + ".json")
    if target.exists():
        old = read_json(target)
        if old.get("signature") != signature:
            raise ValueError("Resume signature mismatch")
        for item in [old["selected"], old.get("best_new")]:
            if item and digest(item["record"]["result_path"]) != item["record"]["result_sha256"]:
                raise ValueError("Changed result on resume")
        return old
    started = time.perf_counter()
    ir = GraphIR.from_path(graph)
    control = validated_control(args.controls / graph.stem / f"simple_p2_n{args.cores}_seed0.json", graph, args.cores)
    generation_start = time.perf_counter()
    candidates, diagnostics = generate_operation_candidates(ir, args.cores, max_candidates=args.max_candidates, seed=args.seed)
    generation_seconds = time.perf_counter() - generation_start
    # Fixed feature-based cap; no selection based on already observed gain.
    timeout = 60 if len(ir.compute_ids) <= 10000 else 180
    rows, selected, best_new = [], control, None
    checkpoint = args.run_dir / "partial" / (graph.stem + ".json")
    previous = read_json(checkpoint) if checkpoint.exists() else None
    if previous and previous["signature"] != signature:
        raise ValueError("Partial signature mismatch")
    prior = {x["plan_hash"]: x for x in previous.get("evaluations", [])} if previous else {}
    for candidate in candidates:
        hashed = object_digest(candidate["plan"])
        if hashed in prior:
            record = prior[hashed]["record"]
            if record["status"] == "success" and digest(record["result_path"]) != record["result_sha256"]:
                raise ValueError("Partial result changed")
        else:
            record = evaluate(graph, candidate["plan"], 2, args.run_dir / "evaluations", timeout=timeout)
        row = {"name": candidate["name"], "plan_hash": hashed, "metadata": candidate["metadata"], "record": record}
        rows.append(row)
        if record["status"] == "success":
            value = {**row, "plan": candidate["plan"]}
            if best_new is None or key(record) < key(best_new["record"]):
                best_new = value
            if key(record) < key(selected["record"]):
                selected = value
        atomic_json(checkpoint, {"signature": signature, "case": graph.stem, "evaluations": rows})
    result = {"signature": signature, "case": graph.stem, "problem": 2, "num_cores": args.cores,
              "status": "success", "scope": "operation exploration with retained historical WCC control; not equal-budget timing",
              "control": control, "best_new": best_new, "selected": selected, "evaluations": rows,
              "generation": diagnostics, "generation_seconds": generation_seconds,
              "per_evaluation_timeout": timeout, "elapsed_seconds": time.perf_counter() - started}
    atomic_json(target, result)
    atomic_json(target.with_suffix(".plan.json"), selected["plan"])
    print(json.dumps({"case": graph.stem, "control": key(control["record"])[0],
                      "selected": key(selected["record"])[0], "new": key(best_new["record"])[0] if best_new else None,
                      "evaluated": len(rows), "failures": sum(x["record"]["status"] != "success" for x in rows)}, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="all")
    parser.add_argument("--cores", type=int, choices=(2, 5), default=5)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-candidates", type=int, default=12)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--controls", type=Path, default=HERE.parent / "solver/runs/full_initial_v1/results")
    args = parser.parse_args(); args.run_dir = args.run_dir.resolve(); args.controls = args.controls.resolve()
    if args.run_dir == DATA.parent or DATA.parent in args.run_dir.parents:
        parser.error("run must be outside official attachments")
    cases = sorted(DATA.glob("case_[0-9][0-9][0-9].json")) if args.cases == "all" else [DATA / f"case_{int(c):03}.json" for c in args.cases.split(",")]
    if args.cases == "all" and len(cases) != 100:
        parser.error("all requires exactly 100 inputs")
    sources = [Path(__file__), HERE / "operation_assign.py", HERE.parent / "探索/operation_heft_probe.py", HERE.parent / "探索/partition_candidates.py"]
    sources += list((HERE.parent / "solver").glob("*.py")) + list(OFFICIAL.glob("*.py"))
    spec = {"sources": {str(p.resolve()): digest(p) for p in sources}, "graphs": {p.name: digest(p) for p in cases},
            "config": digest(DATA / "config.txt"), "seed": args.seed, "cores": args.cores,
            "max_candidates": args.max_candidates, "workers": args.workers, "python": sys.version,
            "timeout_rule": "60s if compute_ops<=10000 else 180s", "controls": str(args.controls),
            "control_sha256": {p.stem: digest(args.controls / p.stem / f"simple_p2_n{args.cores}_seed0.json") for p in cases}}
    signature = object_digest(spec); manifest = args.run_dir / "manifest.json"
    if manifest.exists() and read_json(manifest)["signature"] != signature:
        parser.error("different code/settings in run directory")
    atomic_json(manifest, {"signature": signature, "specification": spec, "command": sys.argv, "platform": platform.platform()})
    for p in sources:
        destination = args.run_dir / "source_snapshot" / p.resolve().relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(p, destination)
    completed, failures = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(run_case, p, args, signature): p for p in cases}
        for job in as_completed(jobs):
            try:
                row = job.result(); completed.append({"case": row["case"], "selected": key(row["selected"]["record"])[0]})
            except Exception as e:
                import traceback
                failures.append({"case": jobs[job].stem, "error": traceback.format_exc()})
            atomic_json(args.run_dir / "progress.json", {"expected": len(cases), "completed": completed, "failures": failures})
    return int(bool(failures) or len(completed) != len(cases))


if __name__ == "__main__":
    raise SystemExit(main())
