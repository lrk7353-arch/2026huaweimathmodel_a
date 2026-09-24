"""Small integration checks; real official CLI smoke runs are separate."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import advanced_solve as advanced


def fake_record(status, makespan=None):
    return {"status": status, "metrics": {} if makespan is None else {"makespan": makespan,
            "data_movement_bytes": {"added_copy_bytes": 0}}, "cache_hit": False,
            "elapsed_seconds": 0.001, "error": None if status == "success" else "test failure"}


class AdvancedTests(unittest.TestCase):
    def test_two_graphs_all_core_counts_structure_determinism(self):
        for case in ("071", "064"):
            ir = advanced.GraphIR.from_path(advanced.DATA / ("case_" + case + ".json"))
            for cores in range(1, 6):
                candidates, failures = advanced.generate_advanced_candidates(ir, cores)
                self.assertFalse(failures, (case, cores, failures))
                self.assertLessEqual(len(candidates), 10)
                self.assertEqual((candidates, failures), advanced.generate_advanced_candidates(ir, cores))
                hashes = set()
                for candidate in candidates:
                    self.assertEqual(set(candidate), {"name", "plan", "metadata"})
                    self.assertEqual(len(candidate["plan"]["core_schedules"]), cores)
                    self.assertTrue(advanced.validate_plan(ir, candidate["plan"]))
                    hashes.add(advanced.object_digest(candidate["plan"]))
                self.assertEqual(len(candidates), len(hashes))

    def test_incumbent_is_prioritized_padded_and_deduplicated(self):
        ir = advanced.GraphIR.from_path(advanced.DATA / "case_071.json")
        incumbent = advanced.single_active_plan(ir.graph, 1)
        candidates, failures = advanced.generate_advanced_candidates(ir, 3, incumbent)
        self.assertFalse(failures)
        self.assertEqual(candidates[0]["name"], "provided_incumbent")
        self.assertEqual(candidates[0]["plan"]["core_schedules"], [[0], [], []])
        self.assertTrue(candidates[0]["metadata"]["requires_fresh_official_evaluation"])
        self.assertNotIn("safe_single_active", [c["name"] for c in candidates])
        with self.assertRaises(ValueError):
            advanced.generate_advanced_candidates(ir, 1, advanced.single_active_plan(ir.graph, 2))

    def test_only_success_can_update_and_all_attempts_budget_are_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(advanced, "evaluate", side_effect=[fake_record("success", 100),
                              fake_record("execution_cycle", 1), fake_record("success", 90)]) as evaluate:
                summary = advanced.solve_advanced(advanced.DATA / "case_071.json", 2, 2, directory,
                                                  max_evaluations=3, timeout=1)
            self.assertEqual(evaluate.call_count, 3)
            self.assertEqual(summary["best"]["record"]["metrics"]["makespan"], 90)
            self.assertEqual(summary["status_counts"], {"success": 2, "execution_cycle": 1})
            self.assertEqual(summary["stop_reason"], "candidate_budget")
            self.assertTrue(summary["not_evaluated_budget"])
            saved = json.loads((Path(summary["session_dir"]) / "summary.json").read_text())
            self.assertEqual(len(saved["evaluations"]), 3)
            source_names = {Path(p).name for p in summary["source_sha256"]}
            self.assertTrue({"advanced_solve.py", "operation_heft_probe.py", "partition_candidates.py"} <= source_names)

    def test_failed_revalidation_does_not_promote_supplied_plan(self):
        ir = advanced.GraphIR.from_path(advanced.DATA / "case_064.json")
        incumbent = advanced.single_active_plan(ir.graph, 1)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(advanced, "evaluate", return_value=fake_record("timeout")) as evaluate:
                result = advanced.solve_advanced(ir.path, 2, 3, directory, max_evaluations=1,
                                                  incumbent_plan=incumbent)
            self.assertEqual(evaluate.call_count, 1)
            self.assertEqual(result["status"], "no_feasible_result")
            self.assertIsNone(result["best"])
            self.assertEqual(result["evaluations"][0]["candidate"], "provided_incumbent")

    def test_fixed_configuration_accepts_comments_but_rejects_different_values(self):
        raw = (advanced.DATA / "config.txt").read_text()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.txt"
            path.write_text(raw + "\n# local copy, same fixed parameters\n")
            self.assertEqual(advanced.check_fixed_config(path), advanced.FIXED_SETTINGS)
            path.write_text(raw.replace("bandwidth 60", "bandwidth 600"))
            with self.assertRaises(ValueError):
                advanced.check_fixed_config(path)

    def test_p1_invalid_budgets_and_official_write_locations_rejected(self):
        with self.assertRaises(ValueError):
            advanced.solve_advanced(advanced.DATA / "case_071.json", 5, 1, "unused")
        with self.assertRaises(ValueError):
            advanced.solve_advanced(advanced.DATA / "case_071.json", 5, 2, "unused", max_evaluations=0)
        with self.assertRaises(ValueError):
            advanced._outside_official(advanced.DATA / "result.json")


class ArtifactPathTests(unittest.TestCase):
    def assert_cli_rejects_before_solve(self, graph, output, directory, incumbent=None):
        argv = [str(graph), "-n", "1", "-p", "2", "--config", str(advanced.DATA / "config.txt"),
                "--run-dir", str(Path(directory) / "records"), "-o", str(output)]
        if incumbent is not None:
            argv += ["--incumbent-plan", str(incumbent)]
        with patch.object(advanced, "solve_advanced") as solve, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                advanced.main(argv)
            self.assertEqual(stopped.exception.code, 2)
            solve.assert_not_called()
        self.assertFalse((Path(directory) / "records").exists())

    def test_regression_advanced_suffix_output_and_summary_are_written_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = Path(directory) / "case.json"
            source = (advanced.DATA / "case_071.json").read_bytes()
            graph.write_bytes(source)
            output = Path(directory) / "foo.advanced.json"
            plan = advanced.single_active_plan(json.loads(source), 1)
            result = {"status": "success", "best": {"plan": plan, "record": fake_record("success", 18919),
                      "candidate": "fixture"}, "evaluated_count": 1, "stop_reason": "all_candidates_evaluated"}
            argv = [str(graph), "-n", "1", "-p", "2", "--config", str(advanced.DATA / "config.txt"),
                    "--run-dir", str(Path(directory) / "records"), "-o", str(output)]
            with patch.object(advanced, "solve_advanced", return_value=result) as solve, redirect_stdout(io.StringIO()):
                self.assertEqual(advanced.main(argv), 0)
                solve.assert_called_once()
            summary = Path(directory) / "foo.advanced.advanced.json"
            self.assertEqual(json.loads(output.read_text()), plan)
            self.assertEqual(json.loads(summary.read_text()), result)
            self.assertEqual(graph.read_bytes(), source)

    def test_regression_derived_summary_cannot_overwrite_external_input_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = Path(directory) / "foo.advanced.json"
            original = (advanced.DATA / "case_071.json").read_bytes()
            graph.write_bytes(original)
            output = Path(directory) / "foo.json"
            self.assert_cli_rejects_before_solve(graph, output, directory)
            self.assertEqual(graph.read_bytes(), original)
            self.assertFalse(output.exists())

    def test_symlink_summary_alias_to_graph_is_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = Path(directory) / "input.json"
            original = (advanced.DATA / "case_071.json").read_bytes()
            graph.write_bytes(original)
            output = Path(directory) / "result.json"
            summary = Path(directory) / "result.advanced.json"
            summary.symlink_to(graph)
            self.assert_cli_rejects_before_solve(graph, output, directory)
            self.assertEqual(graph.read_bytes(), original)
            self.assertTrue(summary.is_symlink())
            self.assertFalse(output.exists())

    def test_symlink_summary_alias_to_plan_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = Path(directory) / "input.json"
            graph.write_bytes((advanced.DATA / "case_071.json").read_bytes())
            output = Path(directory) / "result.json"
            output.write_text("preserve existing plan")
            summary = Path(directory) / "result.advanced.json"
            summary.symlink_to(output)
            self.assert_cli_rejects_before_solve(graph, output, directory)
            self.assertEqual(output.read_text(), "preserve existing plan")
            self.assertTrue(summary.is_symlink())

    def test_plan_and_summary_cannot_overwrite_config_or_incumbent_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = Path(directory) / "graph.json"
            graph.write_text("{}")
            config = Path(directory) / "fixed.json"
            config.write_text("preserve configuration")
            output = Path(directory) / "new.json"
            incumbent = output.with_suffix(".advanced.json")
            incumbent.write_text("preserve incumbent")
            for candidate in (config, incumbent):
                with self.assertRaises(ValueError):
                    advanced.checked_output_paths(candidate, graph, config, incumbent)
            with self.assertRaises(ValueError):
                advanced.checked_output_paths(output, graph, config, incumbent)
            with self.assertRaises(ValueError):
                advanced.checked_output_paths(output, graph, incumbent, config)
            config_alias = Path(directory) / "config_alias.json"
            config_alias.symlink_to(config)
            with self.assertRaises(ValueError):
                advanced.checked_output_paths(config_alias, graph, config, incumbent)
            self.assertEqual(config.read_text(), "preserve configuration")
            self.assertEqual(incumbent.read_text(), "preserve incumbent")

    def test_distinct_resolved_paths_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, summary = advanced.checked_output_paths(root / "result.plan.json", root / "graph.json",
                                                             root / "config.txt", root / "previous.plan.json")
            self.assertEqual(output, (root / "result.plan.json").resolve())
            self.assertEqual(summary, (root / "result.plan.advanced.json").resolve())


if __name__ == "__main__":
    unittest.main()
