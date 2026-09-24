"""Small, deterministic checks for score aggregation and candidate selection."""
from __future__ import annotations

import csv
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SOLVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOLVER))

from common import atomic_json, object_digest, read_json
import benchmark
from solve import solve_one
from summarize import summarize


def record(makespan=None, *, status="success", added=0):
    result = {"status": status, "elapsed_seconds": 0.01, "cache_hit": False}
    if makespan is not None:
        result["metrics"] = {
            "makespan": makespan,
            "data_movement_bytes": {"added_copy_bytes": added},
        }
    return result


def plan(sgid, cores=2):
    return {"node_to_subgraph": {"1": sgid},
            "core_schedules": [[sgid]] + [[] for _ in range(cores - 1)]}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def fixture(self, baselines, *, problems=(1,), cores=(2,), methods=("simple",)):
        manifest = {
            "signature": "test-signature", "scope": "pilot",
            "specification": {
                "inputs": {f"case_{i:03d}.json": "test-hash"
                           for i in range(1, len(baselines) + 1)},
                "problems": list(problems), "cores": list(cores),
                "methods": list(methods), "seed": 0,
            },
        }
        atomic_json(self.directory / "manifest.json", manifest)
        for i, makespan in enumerate(baselines, 1):
            atomic_json(self.directory / "results" / f"case_{i:03d}" / "singlecore.json",
                        record(makespan))
        return manifest

    def put_solution(self, case, makespan=None, *, problem=1, cores=2,
                     method="simple", status="success"):
        rec = record(makespan, status=status)
        best = None if status != "success" else {
            "candidate": "test", "plan": plan(0, cores), "record": rec,
        }
        result = {
            "benchmark_signature": "test-signature", "status": status,
            "elapsed_seconds": 0.1, "generation_seconds": 0.01,
            "candidate_count": 1, "evaluated_count": 1, "official_calls": 1,
            "cache_hits": 0, "stop_reason": "all_candidates_evaluated",
            "evaluations": [{"candidate": "test", "record": rec}], "best": best,
        }
        atomic_json(self.directory / "results" / f"case_{case:03d}" /
                    f"{method}_p{problem}_n{cores}_seed0.json", result)

    def test_plan_hash_preserves_list_order(self):
        first = {"node_to_subgraph": {"1": 0, "2": 1}, "core_schedules": [[0, 1], []]}
        reordered_list = {"node_to_subgraph": {"1": 0, "2": 1}, "core_schedules": [[1, 0], []]}
        self.assertNotEqual(object_digest(first), object_digest(reordered_list))

    def test_plan_hash_conservatively_preserves_mapping_order(self):
        first = {"node_to_subgraph": {"1": 0, "2": 1}, "core_schedules": [[0, 1], []]}
        reordered = {"node_to_subgraph": {"2": 1, "1": 0}, "core_schedules": [[0, 1], []]}
        self.assertNotEqual(object_digest(first), object_digest(reordered))

    def test_duplicate_json_keys_rejected(self):
        path = self.directory / "duplicate.json"
        path.write_text('{"makespan": 100, "makespan": 1}', encoding="utf-8")
        with self.assertRaises(ValueError):
            read_json(path)

    def test_mean_is_mean_of_per_case_ratios(self):
        self.fixture([100, 1000])
        self.put_solution(1, 50)
        self.put_solution(2, 100)
        result = summarize(self.directory)
        aggregate = result["aggregates"][0]
        self.assertTrue(aggregate["complete"])
        self.assertEqual(aggregate["mean_speedup"], 6.0)
        self.assertNotAlmostEqual(aggregate["mean_speedup"], 1100 / 150)

    def test_failed_or_missing_case_withholds_whole_group_mean(self):
        self.fixture([100, 200, 300])
        self.put_solution(1, 50)
        self.put_solution(2, status="no_feasible_result")
        result = summarize(self.directory)
        aggregate = result["aggregates"][0]
        self.assertEqual(result["expected_slots"], 3)
        self.assertEqual(result["successful_slots"], 1)
        self.assertEqual(aggregate["valid_cases"], 1)
        self.assertFalse(aggregate["complete"])
        self.assertIsNone(aggregate["mean_speedup"])
        self.assertEqual(len(result["missing_files"]), 1)
        self.assertEqual(result["candidate_status_counts"]["no_feasible_result"], 1)

    def test_p1_p2_one_core_point_does_not_replace_actual_ratio(self):
        self.fixture([100], problems=(1, 2), cores=(1,))
        for problem in (1, 2):
            self.put_solution(1, 50, problem=problem, cores=1)
        result = summarize(self.directory)
        for aggregate in result["aggregates"]:
            self.assertEqual(aggregate["mean_speedup"], 1.0)
            self.assertEqual(aggregate["mean_actual_ratio"], 2.0)

    def test_p3_one_core_uses_actual_paired_ratio(self):
        self.fixture([100], problems=(2, 3), cores=(1,))
        self.put_solution(1, 100, problem=2, cores=1)
        self.put_solution(1, 50, problem=3, cores=1)
        pair = {
            "case": "case_001", "method": "simple", "num_cores": 1,
            "cells": {"t2_pi2": record(100), "t3_pi2": record(80),
                      "t2_pi3": record(110), "t3_pi3": record(50)},
            "ratios": {"hardware": 1.25, "selection": 1.6, "total": 2.0},
        }
        atomic_json(self.directory / "results" / "case_001" /
                    "simple_cache_pair_n1_seed0.json", pair)
        result = summarize(self.directory)
        p3 = next(a for a in result["aggregates"] if a["problem"] == 3)
        self.assertIsNone(p3["mean_speedup"])
        with (self.directory / "reports" / "cache_pairs.csv").open(encoding="utf-8-sig") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(float(rows[0]["total"]), 2.0)
        self.assertEqual(float(rows[0]["hardware"]) * float(rows[0]["selection"]), 2.0)

    def test_missing_all_p3_pairs_is_incomplete_and_visible(self):
        self.fixture([100], problems=(2, 3), cores=(2,))
        self.put_solution(1, 50, problem=2)
        self.put_solution(1, 40, problem=3)
        result = summarize(self.directory)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["pair_statuses"]), 1)
        self.assertEqual(result["pair_statuses"][0]["status"], "missing")
        self.assertTrue(any("cache_pair" in path for path in result["missing_files"]))
        report = (self.directory / "reports" / "首轮基线实验报告.md").read_text(encoding="utf-8")
        self.assertIn("| simple | 2 | 0/1 |", report)

    def test_failed_p3_pair_cell_withholds_pair_average(self):
        self.fixture([100], problems=(2, 3), cores=(2,))
        self.put_solution(1, 50, problem=2)
        self.put_solution(1, 40, problem=3)
        pair = {
            "case": "case_001", "method": "simple", "num_cores": 2,
            "cells": {"t2_pi2": record(50), "t3_pi2": record(status="timeout"),
                      "t2_pi3": record(60), "t3_pi3": record(40)}, "ratios": {},
        }
        atomic_json(self.directory / "results" / "case_001" /
                    "simple_cache_pair_n2_seed0.json", pair)
        result = summarize(self.directory)
        self.assertFalse(result["complete"])
        self.assertEqual(result["pair_statuses"][0]["status"], "incomplete")
        report = (self.directory / "reports" / "首轮基线实验报告.md").read_text(encoding="utf-8")
        self.assertIn("| simple | 2 | 0/1 |", report)

    def test_benchmark_graph_marks_failed_baseline_and_slot_incomplete(self):
        args = SimpleNamespace(run_dir=self.directory / "run", methods=["simple"],
                               problems=[1], cores=[2], seed=0, timeout=1,
                               config=self.directory / "config.txt",
                               max_evaluations=1, budget_seconds=None)
        failure = {"status": "no_feasible_result", "best": None,
                   "evaluated_count": 1, "elapsed_seconds": 0.01}
        with patch("benchmark.GraphIR.from_path", return_value=SimpleNamespace()), \
                patch("benchmark.evaluate", return_value=record(status="timeout")), \
                patch("benchmark.solve_one", return_value=failure), redirect_stdout(io.StringIO()):
            result = benchmark.run_graph(self.directory / "case_001.json", args,
                                         {"signature": "test-signature"})
        self.assertFalse(result["complete"])
        self.assertEqual(result["baseline_status"], "timeout")
        self.assertEqual(result["failed_slots"], 1)

    def test_benchmark_main_returns_nonzero_for_incomplete_graph(self):
        data = self.directory / "data"
        data.mkdir()
        atomic_json(data / "case_001.json", {"ops": [], "tensors": [], "edges": []})
        (data / "config.txt").write_text("test config\n", encoding="utf-8")
        incomplete = {"case": "case_001", "complete": False, "baseline_status": "timeout",
                      "failed_slots": 1, "failed_pairs": 0, "elapsed_seconds": 0.01}
        with patch("benchmark.run_graph", return_value=incomplete), \
                patch("benchmark.source_manifest", return_value={}), redirect_stdout(io.StringIO()):
            code = benchmark.main(["--data", str(data), "--cases", "001", "--cores", "2",
                                   "--problems", "1", "--methods", "simple", "--workers", "1",
                                   "--run-dir", str(self.directory / "run")])
        self.assertEqual(code, 1)
        progress = read_json(self.directory / "run" / "progress.json")
        self.assertEqual(progress["completed"][0]["complete"], False)

    def test_candidate_selection_ignores_failure_and_breaks_ties_by_movement(self):
        graph = {"ops": [{"id": 1, "op": "ADD"}], "tensors": [], "edges": []}
        graph_path, config_path = self.directory / "graph.json", self.directory / "config.txt"
        atomic_json(graph_path, graph)
        config_path.write_text("test config\n", encoding="utf-8")
        candidates = [
            {"name": "invalid_fast", "plan": plan(1), "metadata": {}},
            {"name": "fast", "plan": plan(2), "metadata": {}},
            {"name": "same_time_less_movement", "plan": plan(3), "metadata": {}},
        ]
        records = [record(1000), record(1, status="invalid"), record(50, added=20), record(50, added=10)]
        with patch("solve.generate_candidates", return_value=candidates), \
                patch("solve.evaluate", side_effect=records):
            result = solve_one(graph_path, 2, 1, "simple", self.directory,
                               config_path=config_path, ir=SimpleNamespace(graph=graph))
        self.assertEqual(result["best"]["candidate"], "same_time_less_movement")
        self.assertEqual(result["evaluated_count"], 4)
        self.assertEqual(result["successful_count"], 3)
        self.assertEqual(result["status_counts"]["invalid"], 1)


if __name__ == "__main__":
    unittest.main()
