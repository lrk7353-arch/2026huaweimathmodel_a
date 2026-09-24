#!/usr/bin/env python3
"""Small synthetic experiments using the unmodified official problem-3 evaluator.

Run: python cache_order_microtests.py
All generated files go beside this script in cache_microtests/.
No official file or fixed hardware parameter is changed.
"""
from pathlib import Path
import hashlib
import json
import platform
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CODE = ROOT / "选题分析/A题附件/code"
CONFIG = ROOT / "选题分析/A题附件/data/config.txt"
OUT = HERE / "cache_microtests"
sys.path.insert(0, str(CODE))
from multicore_cut_evaluate_problem_3 import evaluate_problem_3

PARAMS = dict(bandwidth=60, capacity={"L1": 524288, "UB": 131072},
              cross_core_copy_delay=500, cache_capacity_bytes=1048576,
              cache_bandwidth_bytes_per_cycle=250)


def graph_from_rows(tensor_rows, op_rows, edge_rows):
    return {
        "tensors": [dict(id=i, pos=p, size=s) for i, p, s in tensor_rows],
        "ops": [dict(id=i, op=o, pipe=p, cycles=c) for i, o, p, c in op_rows],
        "edges": [dict(source=a, target=b) for a, b in edge_rows],
    }


def cold_read_graph():
    return graph_from_rows(
        [(i, p, 6000) for i, p in [(1, "DDR"), (2, "UB"), (3, "UB"),
                                    (4, "DDR"), (5, "UB"), (6, "DDR")]],
        [(10, "COPY_IN", "PIPE_MTE2", 1), (11, "ADD", "PIPE_V", 10),
         (12, "COPY_OUT", "PIPE_MTE3", 1), (13, "ADD", "PIPE_V", 10),
         (14, "COPY_OUT", "PIPE_MTE3", 1)],
        [(1, 10), (10, 2), (2, 11), (11, 3), (3, 12), (12, 4),
         (2, 13), (13, 5), (5, 14), (14, 6)],
    )


def reorder_graph(follower_cycles):
    # X: tensor 2 is shared by ops 200 and 201; Y: tensor 4 feeds op 202.
    return graph_from_rows(
        [(1, "DDR", 6000), (2, "UB", 6000), (3, "DDR", 6000), (4, "UB", 6000),
         (5, "UB", 60), (6, "DDR", 60), (7, "UB", 60), (8, "DDR", 60),
         (9, "UB", 60), (10, "DDR", 60)],
        [(100, "COPY_IN", "PIPE_MTE2", 1), (101, "COPY_IN", "PIPE_MTE2", 1),
         (102, "COPY_OUT", "PIPE_MTE3", 1), (103, "COPY_OUT", "PIPE_MTE3", 1),
         (104, "COPY_OUT", "PIPE_MTE3", 1), (200, "ADD", "PIPE_V", 10),
         (201, "ADD", "PIPE_V", follower_cycles), (202, "ADD", "PIPE_V", 10)],
        [(1, 100), (100, 2), (3, 101), (101, 4), (2, 200), (200, 5),
         (5, 102), (102, 6), (2, 201), (201, 7), (7, 103), (103, 8),
         (4, 202), (202, 9), (9, 104), (104, 10)],
    )


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_case(name, graph, plan):
    result = evaluate_problem_3(graph, plan, **PARAMS)
    dump(OUT / f"{name}_graph.json", graph)
    dump(OUT / f"{name}_plan.json", plan)
    dump(OUT / f"{name}_result.json", result)
    return {
        "case": name, "makespan": result["makespan"],
        "cache_stats": result["cache_stats"],
        "cache_events": result["cache_events"],
        "data_movement_bytes": result["data_movement_bytes"],
        "core_schedules": plan["core_schedules"],
    }


def main():
    OUT.mkdir(exist_ok=True)
    rows = [run_case("simultaneous_cold_reads", cold_read_graph(),
                    {"node_to_subgraph": {"11": 0, "13": 1}, "core_schedules": [[0], [1]]})]
    for cycles, prefix in [(10, "short_compute"), (3000, "long_follower_compute")]:
        graph = reorder_graph(cycles)
        for suffix, order in [("simultaneous", [1, 2]), ("staggered", [2, 1])]:
            rows.append(run_case(f"{prefix}_{suffix}", graph,
                                 {"node_to_subgraph": {"200": 0, "201": 1, "202": 2},
                                  "core_schedules": [[0], order]}))
    assert [r["makespan"] for r in rows] == [410, 313, 235, 3211, 3225]
    assert [r["cache_stats"]["copy_in_hits"] for r in rows] == [0, 0, 1, 0, 1]
    for left, right in [(rows[1], rows[2]), (rows[3], rows[4])]:
        assert left["data_movement_bytes"] == right["data_movement_bytes"]
        ins = next(e["time"] for e in right["cache_events"]
                   if e["event"] == "insert" and e["tensor_id"] == 2)
        hit = next(e["time"] for e in right["cache_events"]
                   if e["event"] == "hit" and e["tensor_id"] == 2)
        assert ins == hit == 200  # Retirement before issue permits equality.
    dump(OUT / "summary.json", {"kind": "synthetic mechanism verification; not benchmark performance",
                               "fixed_parameters": PARAMS, "cases": rows})
    files = [CONFIG] + sorted(CODE.glob("*.py"))
    dump(OUT / "provenance.json", {
        "python": platform.python_version(), "script": str(Path(__file__).resolve()),
        "files_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in files},
    })
    for row in rows:
        print(f'{row["case"]}: makespan={row["makespan"]}, '
              f'hits={row["cache_stats"]["copy_in_hits"]}, '
              f'hit_rate_bytes={row["cache_stats"]["hit_rate"]:.6f}')


if __name__ == "__main__":
    main()
