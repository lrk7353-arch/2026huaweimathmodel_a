#!/usr/bin/env python3
"""Predeclared 003/016, widths1/2/4/8 graph-only supplement, zero official calls."""
from pathlib import Path
import sys
import time
import csv

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import p1_depth_bands as fine
from controller import source_hashes
from common import DATA, atomic_json, digest
from graph_ir import GraphIR


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    out = args.run_dir.resolve()
    if out.exists(): raise ValueError("fresh run directory required")
    out.mkdir(parents=True)
    sources = source_hashes()
    for name in ("p1_convex_regions.py", "p1_depth_bands.py", "analyze_p1_depth_bands.py"):
        path = Path(__file__).resolve().parent / name
        sources[str(path)] = digest(path)
    paths = [DATA / ("case_%03d.json" % case) for case in (3, 16)]
    atomic_json(out / "manifest.json", {"scope": "pure structure supplement, not official performance",
        "official_calls": 0, "graphs": {str(p): digest(p) for p in paths}, "source_sha256": sources,
        "num_cores": 5, "seed": 17, "widths": [1, 2, 4, 8], "packing_choices": 1,
        "no_incumbent_lookup_or_pruning": True})
    rows = []
    for path in paths:
        ir = GraphIR.from_path(path)
        start = time.perf_counter()
        candidates, diag = fine.generate_depth_band_candidates(ir, 5, 17)
        elapsed = time.perf_counter() - start
        directory = out / path.stem; directory.mkdir()
        atomic_json(directory / "generation.json", {"candidates": candidates, "diagnostics": diag, "generation_seconds": elapsed})
        for candidate in candidates:
            m = candidate["metadata"]
            plan_file = directory / ("width%d.plan.json" % m["width"])
            atomic_json(plan_file, candidate["plan"])
            row = {"case": path.stem, "width": m["width"], "original_layers": m["packing"]["dependency_levels"],
                "max_original_layer_width": m["packing"]["max_original_layer_width"],
                "initial_groups": m["scc"]["initial_group_count"], "task_count": m["task_count"],
                "scc_groups_eliminated": m["scc"]["groups_eliminated"],
                "max_equal_task_depth_antichain": m["max_equal_task_depth_antichain"],
                "actual_active_cores": m["actual_active_cores"], "plan_lb": m["plan_lower_bound"]["value"],
                "data_dag_compute_bound_path": m["data_dag_compute_bound_path"],
                "boundary_read_plus_write_proxy_bytes": m["boundary_read_plus_write_proxy_bytes"],
                "task_proxy_end_not_official": m["task_proxy_end"], "all_four_generation_seconds": elapsed,
                "plan_path": str(plan_file), "plan_file_sha256": digest(plan_file),
                "plan_sha256": fine.regions.object_digest(candidate["plan"])}
            rows.append(row)
            print("{} width{}: Tasks {}, active {}, LB {}, proxy {:.0f}, {:.2f}s/four".format(
                path.stem,m["width"],m["task_count"],m["actual_active_cores"],row["plan_lb"],row["task_proxy_end_not_official"],elapsed),flush=True)
    for path, expected in sources.items():
        if digest(path) != expected: raise ValueError("source changed")
    with (out / "candidates.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    atomic_json(out / "summary.json", {"completed": True, "official_calls": 0, "source_hashes_verified": True,
        "rows": rows, "interpretation": "Task DAG and plan lower bounds only; low bounds are not achieved makespans; no official evaluation or plan acceptance"})


if __name__ == "__main__": main()
