"""Graph-only P1 cold recipes: whole-WCC packing or tensor micro-regions.

Experimental; no historical input, case-ID routing, or official-code changes.
"""
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import argparse
import hashlib
import json
import signal
import time

from common_run import DATA, GraphIR, atomic_json, evaluate, score, validate_plan
from p1_selective import _toposort_blocks, _assign, block_views, topological_order
from p1_tensor_regions import regions
from p1_joint_regions import capped_regions
from p1_convex_regions import _scc_coarsen
from p1_task_refine import coalesce, split_large


def specifications(ir):
    if max(len(c.nodes) for c in ir.components) <= 2048:
        return [("whole_component_pack", cap) for cap in (512, 1024, 2048, 4096)]
    return [("tensor_merge", 8), ("tensor_merge", 16),
            ("tensor_work_fraction", 8), ("tensor_work_fraction", 16)]


def candidate(ir, cores, spec):
    family, value = spec
    order = topological_order(ir, "stable_id")
    repair = None
    if family == "whole_component_pack":
        blocks = [list(c.nodes) for c in ir.components]
    elif family == "tensor_merge":
        blocks, repair = regions(ir, tensor_threshold=4096, cheap_cycles=64)
    elif family == "tensor_work_fraction":
        ideal = max(ir.total_work_m, ir.total_work_v, 1) / cores
        blocks = capped_regions(ir, ir.compute_ids, ideal/value)
        blocks, repair = _scc_coarsen(ir, blocks, order)
    else:
        raise ValueError("unknown recipe")
    blocks = _toposort_blocks(ir, blocks, order)
    plan, proxy = _assign(ir, blocks, block_views(ir, blocks), cores, cores, "eft")
    merge_cap = value if family in ("whole_component_pack", "tensor_merge") else 8
    plan = coalesce(ir, plan, merge_cap)
    pre_cap = max(Counter(plan["node_to_subgraph"].values()).values())
    # Only the exploratory tensor recipes cap leftovers. Packing retains whole
    # WCCs, including components that individually exceed a small packing cap.
    if family != "whole_component_pack" and pre_cap > 1024:
        plan = split_large(ir, plan, 1024)
    validate_plan(ir, plan)
    sizes = Counter(plan["node_to_subgraph"].values())
    return dict(name=f"recipe_{family}_{value}", plan=plan,
                metadata=dict(family=family, parameter=value, pre_cap_max_task_ops=pre_cap,
                    max_task_ops=max(sizes.values()), task_count=len(sizes),
                    seed_partition_proxy=proxy, repair=repair,
                    scope="graph-only complete candidate; proxy excludes merge/cap changes"))


@contextmanager
def generation_limit(seconds):
    def alarm(_sig, _frame):
        raise TimeoutError("recipe generation deadline")
    previous = signal.signal(signal.SIGALRM, alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def run(case, cores, out, budget=4, seconds=240, timeout=60, generation_timeout=30):
    out = Path(out); out.mkdir(parents=True, exist_ok=False)
    start = time.monotonic(); ir = GraphIR.from_path(DATA/(case+".json"))
    calls, errors, seen, best = [], [], set(), None
    generation = 0.
    specs = specifications(ir)
    def summary(done):
        return dict(case=case, problem=1, cores=cores, num_cores=cores, budget=budget,
            logical_calls=len(calls), calls=calls, best_record=best, complete=done,
            generation_seconds=generation, generation_errors=errors,
            elapsed_seconds=time.monotonic()-start, soft_time_budget=seconds,
            evaluation_timeout=timeout, generation_timeout=generation_timeout,
            specs=specs, scope="cold graph-only recipe probe, not full B24 solver")
    atomic_json(out/"summary.json", summary(False))
    for spec in specs:
        remaining = seconds-(time.monotonic()-start)
        if remaining <= 0 or len(calls) >= budget: break
        t = time.monotonic()
        try:
            with generation_limit(min(generation_timeout, remaining)):
                c = candidate(ir, cores, spec)
        except Exception as exc:
            generation += time.monotonic()-t
            errors.append(dict(spec=spec, error=repr(exc), seconds=time.monotonic()-t))
            atomic_json(out/"summary.json", summary(False)); continue
        generation += time.monotonic()-t
        sig = json.dumps(c["plan"], ensure_ascii=False, separators=(",", ":"))
        if sig in seen: continue
        seen.add(sig)
        remaining = seconds-(time.monotonic()-start)
        if remaining <= 0: break
        r = evaluate(ir.path, c["plan"], 1, out/"evaluations",
                     timeout=min(timeout, remaining), config_path=DATA/"config.txt")
        calls.append(dict(name=c["name"], metadata=c["metadata"], record=r))
        if r["status"] == "success" and (best is None or score(r) < score(best)):
            best = r; atomic_json(out/"best.plan.json", c["plan"])
        atomic_json(out/"summary.json", summary(False))
    result = summary(True); atomic_json(out/"summary.json", result)
    return dict(case=case, problem=1, cores=cores, calls=len(calls),
                best=score(best) if best else None, seconds=result["elapsed_seconds"],
                summary=str(out/"summary.json"), errors=errors)


def _one(args):
    return run(*args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, nargs="+", default=[16, 58, 85, 87])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    atomic_json(args.out/"manifest.json", dict(cases=args.cases, cores=5, budget_per_case=4,
        seconds_per_case=240, timeout=60, generation_timeout=30, workers=args.workers,
        source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        scope="registered developmental recipe probe; choices informed by past data"))
    jobs = [(f"case_{case:03d}", 5, args.out/f"case_{case:03d}") for case in args.cases]
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(_one, jobs):
            results.append(result)
            atomic_json(args.out/"results.json", results)
            print(json.dumps(result, ensure_ascii=False), flush=True)
