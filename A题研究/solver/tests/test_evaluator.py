"""Mechanism and persistence checks against unchanged official code.

Run: python -m unittest discover -s A题研究/solver/tests -p test_evaluator.py -v
All runtime artifacts are temporary. Original data/config/code and prior microtests
are read-only inputs.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SOLVER = Path(__file__).resolve().parents[1]
ROOT = SOLVER.parents[1]
sys.path.insert(0, str(SOLVER))
from evaluator import evaluate

CONFIG = ROOT / "选题分析/A题附件/data/config.txt"
OFFICIAL = ROOT / "选题分析/A题附件/code"
MICRO = ROOT / "A题研究/方案审阅/cache_microtests"


def minimal_graph():
    return {
        "tensors": [{"id": i, "pos": p, "size": 16}
                    for i, p in [(1, "DDR"), (2, "UB"), (3, "UB"), (4, "DDR")]],
        "ops": [dict(id=10, op="COPY_IN", pipe="PIPE_MTE2", cycles=1),
                dict(id=11, op="ADD", pipe="PIPE_V", cycles=4),
                dict(id=12, op="COPY_OUT", pipe="PIPE_MTE3", cycles=1)],
        "edges": [dict(source=a, target=b)
                  for a, b in [(1, 10), (10, 2), (2, 11), (11, 3), (3, 12), (12, 4)]],
    }


class EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="a-evaluator-test-")
        self.base = Path(self.temporary.name)
        self.graph = self.write_graph("minimal", minimal_graph())
        self.plan = {"node_to_subgraph": {"11": 0}, "core_schedules": [[0], []]}

    def tearDown(self):
        self.temporary.cleanup()

    def write_graph(self, name, graph):
        path = self.base / (name + ".json")
        path.write_text(json.dumps(graph), encoding="utf-8")
        return path

    def run_eval(self, problem=3, graph=None, plan=None, **kwargs):
        args = {"config_path": CONFIG, "official_code": OFFICIAL}
        args.update(kwargs)
        return evaluate(graph or self.graph, self.plan if plan is None else plan,
                        problem, self.base / "runs", **args)

    def assert_success(self, record, makespan=None):
        self.assertEqual(record["status"], "success", record)
        self.assertIsNone(record["error"])
        self.assertTrue(Path(record["result_path"]).is_file())
        self.assertTrue(Path(record["record_path"]).is_file())
        if makespan is not None:
            self.assertEqual(record["metrics"]["makespan"], makespan)
        with gzip.open(record["result_path"], "rt", encoding="utf-8") as stream:
            full = json.load(stream)
        self.assertEqual(full["makespan"], record["metrics"]["makespan"])
        return full

    def test_official_minimal_example_all_scenes_and_baseline(self):
        baseline = evaluate(self.graph, None, 0, self.base / "runs", config_path=CONFIG)
        full = self.assert_success(baseline, 6)
        self.assertEqual(full["execution_mode"], "singlecore")
        self.assertIsNone(baseline["plan_path"])
        for problem in (1, 2, 3):
            with self.subTest(problem=problem):
                result = self.run_eval(problem)
                self.assert_success(result, 6)
                self.assertEqual(result["metrics"]["num_cores"], 2)
                self.assertEqual(result["metrics"]["active_cores"], 1)
                self.assertEqual(result["metrics"]["data_movement_bytes"]["added_copy_bytes"], 0)

    def test_five_existing_cache_micrographs(self):
        names = ["simultaneous_cold_reads", "short_compute_simultaneous", "short_compute_staggered",
                 "long_follower_compute_simultaneous", "long_follower_compute_staggered"]
        for name, expected, hits in zip(names, [410, 313, 235, 3211, 3225], [0, 0, 1, 0, 1]):
            with self.subTest(case=name):
                plan = json.loads((MICRO / (name + "_plan.json")).read_text(encoding="utf-8"))
                result = self.run_eval(graph=MICRO / (name + "_graph.json"), plan=plan)
                self.assert_success(result, expected)
                self.assertEqual(result["metrics"]["cache_stats"]["copy_in_hits"], hits)
                if hits:
                    full = self.assert_success(result)
                    insert = next(e["time"] for e in full["cache_events"]
                                  if e["event"] == "insert" and e["tensor_id"] == 2)
                    hit = next(e["time"] for e in full["cache_events"]
                               if e["event"] == "hit" and e["tensor_id"] == 2)
                    self.assertEqual((insert, hit), (200, 200))

    def test_zero_hit_p2_p3(self):
        name = "simultaneous_cold_reads"
        graph = MICRO / (name + "_graph.json")
        plan = json.loads((MICRO / (name + "_plan.json")).read_text(encoding="utf-8"))
        p2 = self.run_eval(2, graph, plan)
        p3 = self.run_eval(3, graph, plan)
        self.assert_success(p2, 410)
        self.assert_success(p3, 410)
        self.assertEqual(p3["metrics"]["cache_stats"]["hits"], 0)
        self.assertEqual(p2["metrics"]["data_movement_bytes"], p3["metrics"]["data_movement_bytes"])

    def test_success_cache_and_missing_result_recovery(self):
        first = self.run_eval()
        second = self.run_eval()
        self.assert_success(first, 6)
        self.assert_success(second, 6)
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["result_path"], second["result_path"])
        self.assertNotEqual(first["record_path"], second["record_path"])
        Path(first["result_path"]).unlink()
        third = self.run_eval()
        self.assert_success(third, 6)
        self.assertFalse(third["cache_hit"])
        self.assertNotEqual(first["result_path"], third["result_path"])

    def test_corrupt_compressed_result_is_not_reused(self):
        first = self.run_eval()
        self.assert_success(first)
        Path(first["result_path"]).write_bytes(b"not a gzip result")
        second = self.run_eval()
        self.assert_success(second, 6)
        self.assertFalse(second["cache_hit"])

    def test_plan_list_order_and_scene_have_distinct_keys(self):
        name = "short_compute_simultaneous"
        graph = MICRO / (name + "_graph.json")
        plan = json.loads((MICRO / (name + "_plan.json")).read_text(encoding="utf-8"))
        first = self.run_eval(3, graph, plan)
        changed = copy.deepcopy(plan)
        changed["core_schedules"][1].reverse()
        second = self.run_eval(3, graph, changed)
        third = self.run_eval(2, graph, plan)
        self.assertEqual(len({r["cache_key"] for r in (first, second, third)}), 3)
        self.assert_success(first, 313)
        self.assert_success(second, 235)

    def test_plan_object_order_is_not_silently_normalized(self):
        first = self.run_eval()
        changed = {"core_schedules": self.plan["core_schedules"],
                   "node_to_subgraph": self.plan["node_to_subgraph"]}
        second = self.run_eval(plan=changed)
        self.assert_success(second, 6)
        self.assertNotEqual(first["cache_key"], second["cache_key"])

    def test_timeout_retains_logs_and_does_not_cache_failure(self):
        failed = self.run_eval(timeout=0.000001)
        self.assertEqual(failed["status"], "timeout", failed)
        self.assertFalse(failed["cache_hit"])
        self.assertTrue(Path(failed["stderr_path"]).is_file())
        self.assertTrue(Path(failed["plan_path"]).is_file())
        success = self.run_eval(timeout=10)
        self.assert_success(success, 6)
        self.assertFalse(success["cache_hit"])
        self.assertEqual(failed["cache_key"], success["cache_key"])

    def test_invalid_plan_retains_traceback_and_is_retried(self):
        invalid = {"node_to_subgraph": {}, "core_schedules": [[], []]}
        first = self.run_eval(plan=invalid)
        second = self.run_eval(plan=invalid)
        for record in (first, second):
            self.assertEqual(record["status"], "invalid_plan", record)
            self.assertFalse(record["cache_hit"])
            self.assertIsNone(record["result_path"])
            stderr = Path(record["stderr_path"]).read_text(encoding="utf-8")
            self.assertIn("Traceback", stderr)
            self.assertIn("exactly cover", stderr)
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])

    def test_missing_config_never_uses_default_numbers(self):
        for kwargs in ({"config_path": self.base / "absent.txt"}, {"config_path": None}):
            with self.subTest(kwargs=kwargs):
                record = self.run_eval(**kwargs)
                self.assertEqual(record["status"], "runtime_error", record)
                self.assertIn("configuration file not found", record["error"])
                self.assertTrue(Path(record["record_path"]).is_file())

    def test_singlecore_rejects_user_optimized_plan(self):
        record = self.run_eval(problem=0)
        self.assertEqual(record["status"], "invalid_plan", record)
        self.assertIn("requires plan=None", record["error"])

    def test_malformed_graph_is_runtime_error_with_worker_traceback(self):
        graph = self.base / "broken.json"
        graph.write_text('{"ops": [}', encoding="utf-8")
        result = self.run_eval(graph=graph)
        self.assertEqual(result["status"], "runtime_error", result)
        self.assertEqual(result["error_stage"], "load_graph")
        self.assertIn("Traceback", Path(result["stderr_path"]).read_text(encoding="utf-8"))

    def test_concurrent_same_key_attempts_are_independent_and_atomic(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            records = list(pool.map(lambda _: self.run_eval(), range(3)))
        self.assertEqual(len({r["attempt_id"] for r in records}), 3)
        self.assertEqual(len({r["cache_key"] for r in records}), 1)
        for record in records:
            self.assert_success(record, 6)
        cached = self.run_eval()
        self.assert_success(cached, 6)
        self.assertTrue(cached["cache_hit"])

    def test_capacity_failure(self):
        graph = minimal_graph()
        graph["tensors"][0]["size"] = 131073
        graph["tensors"][1]["size"] = 131073
        record = self.run_eval(graph=self.write_graph("too_large", graph))
        self.assertEqual(record["status"], "capacity_failure", record)
        self.assertIn("capacity", record["error"])

    def test_contracted_cycle_is_preserved_as_failure(self):
        graph = {
            "tensors": [{"id": i, "pos": "DDR" if i in (1, 6) else "UB", "size": 16}
                        for i in range(1, 7)],
            "ops": [dict(id=100, op="COPY_IN", pipe="PIPE_MTE2", cycles=1)]
                   + [dict(id=i, op="ADD", pipe="PIPE_V", cycles=4) for i in (101, 102, 103)]
                   + [dict(id=104, op="COPY_OUT", pipe="PIPE_MTE3", cycles=1)],
            "edges": [dict(source=a, target=b) for a, b in
                      [(1, 100), (100, 2), (2, 101), (101, 3), (3, 102), (102, 4),
                       (4, 103), (103, 5), (5, 104), (104, 6)]],
        }
        plan = {"node_to_subgraph": {"101": 0, "102": 1, "103": 0}, "core_schedules": [[0], [1]]}
        record = self.run_eval(graph=self.write_graph("chain", graph), plan=plan)
        self.assertEqual(record["status"], "execution_cycle", record)
        self.assertIn("cycle", record["error"])

    def test_provenance_rss_and_no_trace_output(self):
        record = self.run_eval()
        self.assert_success(record)
        expected = {p.name for p in OFFICIAL.glob("*.py")}
        self.assertEqual(set(record["hashes"]["official_py_sha256"]), expected)
        self.assertEqual(len(record["hashes"]["worker_sha256"]), 64)
        self.assertEqual(len(record["hashes"]["wrapper_sha256"]), 64)
        if sys.platform in ("darwin", "linux"):
            self.assertGreater(record["peak_memory_bytes"], 0)
        self.assertEqual(record["metrics"]["peak_memory_bytes"], record["peak_memory_bytes"])
        self.assertFalse(list(Path(record["attempt_dir"]).glob("*trace*")))


if __name__ == "__main__":
    unittest.main()
