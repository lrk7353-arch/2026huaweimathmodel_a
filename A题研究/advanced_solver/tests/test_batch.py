"""Batch protocol tests. Fake child outputs only; zero official evaluations."""
import copy
import gzip
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from advanced_solver import batch


class FakeChild:
    def __init__(self, mode="success"):
        self.mode = mode
        self.commands = []
        self.lock = threading.Lock()

    def __call__(self, command, **kwargs):
        with self.lock:
            self.commands.append(list(command))
        self.assert_empty_attempt(command)
        attempt = Path(command[command.index("--run-dir") + 1])
        signature = batch.read_json(Path(str(attempt) + ".launch.json"))["signature"]
        attempt.mkdir()
        if self.mode == "crash":
            return subprocess.CompletedProcess(command, 2)
        if self.mode == "raise":
            raise OSError("fake child launch failed")
        if self.mode == "minimal_timeout":
            supervisor = {"timed_out": True, "child_returncode": -9, "walltime": 0.1}
            batch.atomic_json(attempt / "supervisor.json", supervisor)
            batch.atomic_json(attempt / "summary.json", {"status": "no_feasible_result", "best": None,
                "completed": False, "state": "supervisor_timeout", "supervisor": supervisor})
            return subprocess.CompletedProcess(command, 124)
        problem, cores = signature["problem"], signature["num_cores"]
        initial = signature["initial_plan_path"]
        plan = batch.read_json(initial) if initial else {"node_to_subgraph": {"1": 0}, "core_schedules": [[0]] + [[] for _ in range(cores - 1)]}
        plan_path = attempt / "evaluation/plan.json"
        batch.atomic_json(plan_path, plan)
        official_path = attempt / "evaluation/official_result.json.gz"
        makespan = 100 + problem + cores
        with gzip.open(official_path, "wt", encoding="utf-8") as stream:
            json.dump({"scene": "A" if problem == 1 else "B", "problem": problem, "makespan": makespan, "num_cores": cores}, stream)
        sources = {Path(p).name: h for p, h in signature["source_sha256"].items() if Path(p).parent == batch.OFFICIAL}
        record = {"status": "success", "problem": problem, "plan_path": str(plan_path), "result_path": str(official_path),
                  "result_sha256": batch.digest(official_path), "metrics": {"makespan": makespan, "num_cores": cores},
                  "hashes": {"graph_sha256": signature["graph_sha256"], "config_sha256": signature["config_sha256"],
                             "plan_sha256": batch.digest(plan_path), "official_py_sha256": sources}}
        row = {"stage": "initial" if initial else "component", "name": "fake_child_fixture",
               "plan_sha256": batch.object_digest(plan), "record": record}
        result = {k: signature[k] for k in ("graph_sha256", "config_sha256", "source_sha256", "problem", "num_cores",
                  "profile", "seed", "budgets", "max_rounds", "initial_plan_sha256")}
        result.update({"total_evaluations": signature["max_evaluations"], "per_evaluation_timeout": signature["timeout"],
                       "status": "success", "completed": self.mode != "unfinished", "state": "supervisor_timeout" if self.mode == "unfinished" else "finished",
                       "stop_reason": "hard_wall_budget" if self.mode == "unfinished" else "stages_exhausted_or_stagnated",
                       "best": {**row, "plan": plan}, "evaluations": [row], "evaluated_count": 1, "official_calls": 1})
        batch.atomic_json(attempt / "best.plan.json", plan)
        batch.atomic_json(attempt / "summary.json", result)
        return subprocess.CompletedProcess(command, 124 if self.mode == "unfinished" else 0)

    @staticmethod
    def assert_empty_attempt(command):
        attempt = Path(command[command.index("--run-dir") + 1])
        if attempt.exists():
            raise AssertionError("batch made fresh child run directory nonempty")


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.data = self.base / "data"
        self.data.mkdir()
        for i in (1, 2):
            (self.data / ("case_{:03d}.json".format(i))).write_text('{"ops":[{"id":1}]}')
        (self.data / "config.txt").write_text("test config")
        self.entry = self.base / "fake_entry.py"
        self.entry.write_text("# Not executed: protocol fixture only\n")
        self.official = self.base / "official"
        self.official.mkdir()
        official_file = self.official / "fixture.py"
        official_file.write_text("# never executed\n")
        self.sources = {str(official_file): batch.digest(official_file), str(self.entry): batch.digest(self.entry)}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(batch, "OFFICIAL", self.official).start()
        mock.patch.object(batch, "source_hashes", return_value=self.sources).start()
        self.settings = {"cases": [1], "problems": [2], "cores": [5], "data_dir": str(self.data),
            "config": str(self.data / "config.txt"), "profile": "full", "seed": 17,
            "budgets": {"component": 4, "operation": 8, "trace": 12, "cache": 0},
            "max_rounds": 3, "max_evaluations": 24, "timeout": 60.0, "wall_budget": None,
            "workers": 1, "warm_p2": False, "exploratory": False, "evaluation_dir": None}

    def run_fixture(self, runner, *, settings=None, resume=False, out=None):
        return batch.run_batch(settings or self.settings, out or self.base / "run", resume=resume,
            runner=runner, solve_entry=self.entry, supervisor_entry=self.entry)

    def test_selection_parser(self):
        self.assertEqual(batch.parse_numbers("069,1,case_069", 100, allow_all=True), [69, 1])
        self.assertEqual(len(batch.parse_numbers("all", 100, allow_all=True)), 100)
        for bad in ("", "0", "101", "1,,2", "一", "1;2"):
            with self.assertRaises(ValueError):
                batch.parse_numbers(bad, 100, allow_all=True)

    def test_completed_slot_resume_reuses_only_after_full_hash_verification(self):
        runner = FakeChild()
        first = self.run_fixture(runner)
        self.assertTrue(first["all_searches_completed"])
        self.assertTrue(first["all_slots_feasible"])
        resumed = self.run_fixture(runner, resume=True)
        self.assertEqual(len(runner.commands), 1)
        self.assertEqual(resumed["reused_count"], 1)
        self.assertEqual(resumed["slots"][0]["summary_validation"], "full_hash_and_result_verification")

    def test_corrupted_gzip_or_plan_refuses_resume_without_launch(self):
        for corruption in ("gzip", "plan"):
            out = self.base / corruption
            runner = FakeChild()
            first = self.run_fixture(runner, out=out)
            attempt = Path(first["slots"][0]["attempt_dir"])
            target = attempt / "evaluation" / ("official_result.json.gz" if corruption == "gzip" else "plan.json")
            target.write_bytes(b"corrupted")
            resumed = self.run_fixture(runner, resume=True, out=out)
            self.assertEqual(resumed["slots"][0]["outcome"], "resume_rejected")
            self.assertEqual(len(runner.commands), 1)

    def test_changed_graph_config_settings_or_sources_refuse_root_resume(self):
        runner = FakeChild()
        self.run_fixture(runner)
        changed = copy.deepcopy(self.settings)
        changed["max_evaluations"] = 25
        with self.assertRaises(ValueError):
            self.run_fixture(runner, settings=changed, resume=True)
        graph = self.data / "case_001.json"
        original = graph.read_text()
        graph.write_text("changed")
        with self.assertRaises(ValueError):
            self.run_fixture(runner, resume=True)
        graph.write_text(original)
        self.sources[str(self.entry)] = "different"
        with self.assertRaises(ValueError):
            self.run_fixture(runner, resume=True)
        self.assertEqual(len(runner.commands), 1)

    def test_unfinished_feasible_is_not_complete_and_retries_fresh_attempt(self):
        runner = FakeChild("unfinished")
        first = self.run_fixture(runner)
        self.assertEqual(first["slots"][0]["outcome"], "unfinished_feasible")
        self.assertFalse(first["all_searches_completed"])
        self.assertTrue(first["all_slots_feasible"])
        runner.mode = "success"
        resumed = self.run_fixture(runner, resume=True)
        self.assertTrue(resumed["all_searches_completed"])
        self.assertEqual(len(runner.commands), 2)
        self.assertTrue(resumed["slots"][0]["attempt_dir"].endswith("attempt_0002"))
        self.assertTrue((self.base / "run/slots/case_001/p2_n5/attempt_0001/summary.json").exists())

    def test_minimal_supervisor_failure_is_retained_then_retried(self):
        runner = FakeChild("minimal_timeout")
        settings = {**self.settings, "wall_budget": 1.0}
        first = self.run_fixture(runner, settings=settings)
        self.assertEqual(first["slots"][0]["outcome"], "unfinished_no_feasible")
        self.assertEqual(first["slots"][0]["summary_validation"], "minimal_failure_no_result")
        self.assertIsNone(first["slots"][0]["evaluated_count"])
        runner.mode = "success"
        second = self.run_fixture(runner, settings=settings, resume=True)
        self.assertTrue(second["all_searches_completed"])
        self.assertEqual(len(runner.commands), 2)

    def test_all_slots_reported_despite_case_child_failures(self):
        settings = {**self.settings, "cases": [1, 2], "problems": [1, 2, 3], "cores": [1, 3], "workers": 2}
        runner = FakeChild("crash")
        result = self.run_fixture(runner, settings=settings)
        self.assertEqual(result["planned_slots"], 12)
        self.assertEqual(result["reported_slots"], 12)
        self.assertEqual(len(runner.commands), 12)
        self.assertEqual(result["feasible_count"], 0)
        self.assertTrue(all(r["outcome"] == "slot_failed" for r in result["slots"]))

    def test_warm_p2_dependency_order_same_n_and_charged_initial_trial(self):
        settings = {**self.settings, "cases": [1, 2], "problems": [3, 2], "cores": [1, 3], "workers": 2, "warm_p2": True}
        runner = FakeChild()
        result = self.run_fixture(runner, settings=settings)
        self.assertEqual(result["reported_slots"], 8)
        self.assertTrue(result["all_slots_feasible"])
        for case in ("case_001", "case_002"):
            commands = [c for c in runner.commands if Path(c[3]).stem == case]
            self.assertEqual([int(c[c.index("-p") + 1]) for c in commands], [2, 3, 2, 3])
            for command in commands:
                if command[command.index("-p") + 1] == "3":
                    path = Path(command[command.index("--incumbent-plan") + 1])
                    n = command[command.index("-n") + 1]
                    self.assertIn("p2_n" + n, str(path))
        self.assertTrue(all(r.get("warm_initial_evaluation_is_charged") for r in result["slots"] if r["problem"] == 3))

    def test_supervisor_wall_budget_and_shared_cache_are_forwarded(self):
        cache = str(self.base / "exact_cache")
        settings = {**self.settings, "wall_budget": 0.5, "evaluation_dir": cache}
        runner = FakeChild()
        self.run_fixture(runner, settings=settings)
        command = runner.commands[0]
        self.assertEqual(command[command.index("--wall-budget") + 1], "0.5")
        self.assertEqual(command[command.index("--evaluation-dir") + 1], cache)
        self.assertEqual(command[command.index("--trace-cap") + 1], "12")

    def test_untracked_or_symlink_attempt_path_is_not_overwritten(self):
        out = self.base / "run"
        runner = FakeChild()
        self.run_fixture(runner)
        slot = out / "slots/case_001/p2_n5"
        target = self.base / "outside"
        target.mkdir()
        # Rename the original launch out of discovery, but leave its attempt.
        (slot / "attempt_0001.launch.json").rename(slot / "old_launch.json")
        result = self.run_fixture(runner, resume=True)
        self.assertEqual(result["slots"][0]["outcome"], "resume_rejected")
        self.assertEqual(len(runner.commands), 1)
        self.assertEqual(list(target.iterdir()), [])
        slot.rename(slot.with_name("original_slot"))
        slot.symlink_to(target, target_is_directory=True)
        result = self.run_fixture(runner, resume=True)
        self.assertEqual(result["slots"][0]["outcome"], "resume_rejected")
        self.assertEqual(len(runner.commands), 1)
        self.assertEqual(list(target.iterdir()), [])

    def test_warm_profile_policy_rejected_before_any_run(self):
        args = ["--cases", "1", "--problems", "2,3", "--cores", "5", "--profile", "trace",
                "--warm-p2", "--run-dir", str(self.base / "unused"), "--data-dir", str(self.data)]
        with mock.patch.object(batch, "run_batch") as run, mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            batch.main(args)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
