import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from advanced_solver import engine
from advanced_solver.engine import Search, generate_coarse_p1_candidates
from common import DATA, read_json, object_digest, single_active_plan, atomic_json
from graph_ir import GraphIR
from plan import validate_plan
from advanced_solver.supervisor import recover, supervise
from advanced_solver.solve import parser, output_path


class EngineTests(unittest.TestCase):
    def test_p1_generalizes_to_all_core_counts_and_preserves_graph(self):
        ir = GraphIR.from_path(DATA / "case_071.json")
        before = copy.deepcopy(ir.graph)
        for n in range(1, 6):
            values, diagnostics = generate_coarse_p1_candidates(ir, n, 12, 17)
            self.assertTrue(values)
            for c in values:
                validate_plan(ir, c["plan"])
                self.assertEqual(n, len(c["plan"]["core_schedules"]))
        self.assertEqual(before, ir.graph)

    def test_failed_trial_never_replaces_best_and_cache_still_counts(self):
        ir = GraphIR.from_path(DATA / "case_071.json")
        values, _ = generate_coarse_p1_candidates(ir, 5, 4, 17)
        records = [{"status": "success", "metrics": {"makespan": 50}, "cache_hit": True},
                   {"status": "timeout", "metrics": None},
                   {"status": "success", "metrics": {"makespan": 40}}]
        with tempfile.TemporaryDirectory() as td:
            search = Search(DATA / "case_071.json", 5, 1, Path(td) / "run", total_evaluations=2)
            with patch.object(engine, "evaluate", side_effect=records) as evaluator:
                self.assertTrue(search.trial(values[0], "a"))
                self.assertFalse(search.trial(values[0], "duplicate"))
                self.assertTrue(search.trial(values[1], "a"))
                self.assertFalse(search.trial(values[2], "a"))
                self.assertEqual(2, evaluator.call_count)
            self.assertEqual(50, search.best["record"]["metrics"]["makespan"])
            out = search.checkpoint("test")
            self.assertEqual(2, out["evaluated_count"])
            self.assertEqual(1, out["cache_hits"])
            self.assertEqual({"success": 1, "timeout": 1}, out["status_counts"])

    def test_iterative_stage_uses_new_incumbent_and_stops_on_stagnation(self):
        ir = GraphIR.from_path(DATA / "case_071.json")
        values, _ = generate_coarse_p1_candidates(ir, 5, 4, 17)
        with tempfile.TemporaryDirectory() as td:
            search = Search(DATA / "case_071.json", 5, 2, Path(td) / "run")
            rows = [{"status": "success", "metrics": {"makespan": t}} for t in (100, 90, 95)]
            seen = []
            def generate(r, n):
                seen.append(search.best["record"]["metrics"]["makespan"])
                return [values[r + 1]], {"round": r}
            with patch.object(engine, "evaluate", side_effect=rows):
                search.trial(values[0], "initial")
                search.stage("trace", generate, 12, iterative=True)
            self.assertEqual([100, 90], seen)
            self.assertEqual(90, search.best["record"]["metrics"]["makespan"])
            self.assertEqual(2, len(search.stages))

    def test_used_directory_and_aliased_output_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run"
            Search(DATA / "case_071.json", 5, 2, run)
            with self.assertRaises(ValueError):
                Search(DATA / "case_071.json", 5, 2, run)
            args = parser().parse_args([str(DATA / "case_071.json"), "-n", "5", "-p", "2", "--run-dir", str(run), "-o", str(run / "summary.json")])
            with self.assertRaises(ValueError):
                output_path(args)

    def test_real_official_checkpoint_recovers_and_tampered_plan_rejected(self):
        fixture = read_json(HERE / "runs/operation_smoke_v1/results/case_071.json")["selected"]
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run"
            search = Search(DATA / "case_071.json", 5, 2, run)
            search.best = {"name": "actual_fixture", "plan": fixture["plan"], "record": fixture["record"]}
            search.checkpoint("searching")
            result = recover(run, {"timed_out": True, "child_returncode": -9})
            self.assertFalse(result["completed"])
            self.assertEqual("success", result["status"])
            self.assertEqual(9374, result["best"]["record"]["metrics"]["makespan"])
            result["best"]["plan"]["core_schedules"] = [[]] * 5
            atomic_json(run / "summary.json", result)
            with self.assertRaises(ValueError):
                recover(run, {"timed_out": True, "child_returncode": -9})

    def test_supervisor_terminates_hung_process(self):
        with tempfile.TemporaryDirectory() as td:
            outcome = supervise([sys.executable, "-c", "import time; time.sleep(30)"], .15, Path(td) / "child.log")
            self.assertTrue(outcome["timed_out"])
            self.assertLess(outcome["wall_seconds"], 2)
            self.assertNotEqual(0, outcome["child_returncode"])


if __name__ == "__main__":
    unittest.main()
