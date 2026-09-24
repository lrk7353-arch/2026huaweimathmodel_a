#!/usr/bin/env python3
"""Bounded graph-only report for convex-region candidates; zero official calls."""
import argparse
import csv
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import p1_convex_regions as convex
from p1_selective import generate_selective_candidates
from controller import source_hashes
from common import DATA, atomic_json, digest
from graph_ir import GraphIR


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="002,003,005,009,016")
    parser.add_argument("--num-cores", type=int, default=5)
    parser.add_argument("--max-candidates", type=int, default=12)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    out = args.run_dir.resolve()
    if out.exists(): raise ValueError("fresh output directory required")
    out.mkdir(parents=True)
    sources = source_hashes()
    for name in ("p1_convex_regions.py", "analyze_p1_convex.py"):
        path = Path(__file__).resolve().parent / name
        sources[str(path)] = digest(path)
    ids = [int(value) for value in args.cases.split(",")]
    manifest = {"kind": "pure_structural_analysis", "official_calls": 0, "source_sha256": sources,
        "cases": ids, "num_cores": args.num_cores, "max_candidates": args.max_candidates,
        "seed": args.seed, "comparison": "frozen selective phase_band family, generated with cap100 so all unique variants are available; LB comparisons are not performance comparisons",
        "graph_sha256": {str(c): digest(DATA / ("case_%03d.json" % c)) for c in ids}}
    atomic_json(out / "manifest.json", manifest)
    rows, all_candidates = [], []
    for case in ids:
        path = DATA / ("case_%03d.json" % case)
        load = time.perf_counter(); ir = GraphIR.from_path(path); load_time = time.perf_counter() - load
        start = time.perf_counter()
        candidates, diagnostics = convex.generate_convex_candidates(ir, args.num_cores, args.max_candidates, args.seed)
        seconds = time.perf_counter() - start
        directory = out / ("case_%03d" % case); directory.mkdir()
        atomic_json(directory / "generation.json", {"diagnostics": diagnostics, "candidates": candidates})
        for index, candidate in enumerate(candidates):
            atomic_json(directory / ("candidate_%02d.plan.json" % index), candidate["plan"])
            metadata = candidate["metadata"]; st = metadata["structure"]
            all_candidates.append({"case": case, "index": index, "name": candidate["name"],
                "metric": metadata["band_metric"], "band_multiplier": metadata["band_multiplier"],
                "active_core_cap": metadata["task_active_core_cap"], "actual_active_cores": sum(bool(v) for v in candidate["plan"]["core_schedules"]),
                "task_count": st["task_count"], "largest_task_ops": st["largest_task_ops"],
                "initial_group_count": metadata["scc"]["initial_group_count"], "scc_groups_eliminated": metadata["scc"]["groups_eliminated"],
                "collapse_fraction": metadata["scc"]["group_collapse_fraction"], "max_antichain_witness": st["max_equal_depth_antichain"],
                "envelope_work_over_path": st["envelope_work_over_path"], "plan_lb": metadata["plan_lower_bound"]["value"],
                "proxy_end_not_official": metadata["task_proxy_end"], "cross_task_tensor_route_bytes": st["cross_task_tensor_route_bytes"],
                "plan_sha256": convex.object_digest(candidate["plan"]), "plan_path": str(directory / ("candidate_%02d.plan.json" % index))})
        base_start = time.perf_counter()
        old, old_diag = generate_selective_candidates(ir, args.num_cores, max_candidates=100, seed=args.seed)
        old_phase = [c for c in old if c["metadata"]["partition"] == "phase_band"]
        old_structure = []
        for candidate in old_phase:
            mapping = candidate["plan"]["node_to_subgraph"]
            sgs = sorted(set(mapping.values()))
            blocks = [[int(op) for op, sg in mapping.items() if sg == bid] for bid in sgs]
            view = convex.block_views(ir, blocks)
            bound = candidate["metadata"]["plan_lower_bound"]
            structure = convex._partition_diagnostics(ir, blocks, view, [bound["task_duration_bounds"][bid] for bid in sgs])
            old_structure.append({"name": candidate["name"], "plan_lb": bound["value"], "structure": structure})
        atomic_json(directory / "frozen_phase_comparison.json", {"phase_candidates": old_structure, "diagnostics": old_diag})
        baseline_seconds = time.perf_counter() - base_start
        local = [r for r in all_candidates if r["case"] == case]
        best = min(local, key=lambda r: (r["plan_lb"], r["name"])) if local else None
        row = {"case": case, "compute_ops": len(ir.compute_ids), "components": len(ir.components),
               "load_seconds": load_time, "generation_seconds": seconds, "comparison_generation_seconds": baseline_seconds,
               "raw_plans": diagnostics["raw_plan_count"], "generated_unique": diagnostics["generated_unique"],
               "selected": len(candidates), "generation_failures": diagnostics["generation_failures"],
               "task_count_min": min((r["task_count"] for r in local), default=None),
               "task_count_max": max((r["task_count"] for r in local), default=None),
               "minimum_plan_lb": best["plan_lb"] if best else None, "minimum_lb_candidate": best,
               "max_antichain_witness": max((r["max_antichain_witness"] for r in local), default=0),
               "max_envelope_work_over_path": max((r["envelope_work_over_path"] for r in local), default=0),
               "old_phase_count": len(old_phase), "old_phase_min_lb": min((r["plan_lb"] for r in old_structure), default=None),
               "old_phase_max_antichain_witness": max((r["structure"]["max_equal_depth_antichain"] for r in old_structure), default=0),
               "old_phase_max_envelope_work_over_path": max((r["structure"]["envelope_work_over_path"] for r in old_structure), default=0)}
        rows.append(row)
        atomic_json(out / "progress.json", {"completed_cases": rows, "official_calls": 0})
        print("case{:03d}: {} unique, {} selected, Tasks {}..{}, minLB {}, {:.2f}s".format(
            case, row["generated_unique"], row["selected"], row["task_count_min"], row["task_count_max"], row["minimum_plan_lb"], seconds), flush=True)
    for path, expected in sources.items():
        if digest(path) != expected: raise ValueError("source changed: " + path)
    for case in ids:
        if digest(DATA / ("case_%03d.json" % case)) != manifest["graph_sha256"][str(case)]:
            raise ValueError("graph changed")
    atomic_json(out / "summary.json", {"completed": True, "official_calls": 0, "source_and_input_hashes_verified": True,
                                      "scope": manifest["kind"], "cases": rows})
    if all_candidates:
        with (out / "candidates.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(all_candidates[0])); writer.writeheader(); writer.writerows(all_candidates)


if __name__ == "__main__":
    main()
