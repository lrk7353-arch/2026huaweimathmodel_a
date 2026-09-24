"""Controller tests and synthetic structural checks; zero official evaluations."""
import copy
import gzip
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("standalone_refine", HERE / "refine.py")
refine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refine)
from advanced_solver.tests.test_trace_refine import fixture as trace_fixture, synthetic_trace
from advanced_solver.tests.test_cache_refine import fifo_mock


def plan_variant(number, cores=1):
    return {"node_to_subgraph": {str(i): number * 10 + i for i in range(1, 5)},
            "core_schedules": [[number * 10 + i for i in range(1, 5)]] + [[] for _ in range(cores - 1)]}


def candidate(number, cores=1):
    return {"name": "v" + str(number), "plan": plan_variant(number, cores), "metadata": {}}


class FakeEvaluator:
    """Fake records are labelled in filenames and used only for controller tests."""
    def __init__(self, search, results, interrupted=False, cache_hit=False):
        self.search, self.results, self.calls = search, results, 0
        self.interrupted, self.cache_hit = interrupted, cache_hit

    def __call__(self, graph, plan, problem, evaluation_dir, **kwargs):
        search = self.search
        checkpoint = refine.read_json(search.run_dir / "checkpoint.json")
        assert checkpoint["state"] == "evaluation_pending"
        assert checkpoint["logical_calls"] == self.calls + 1
        assert checkpoint["evaluations"][-1]["state"] == "pending"
        assert checkpoint["evaluations"][-1]["plan_sha256"] == refine.object_digest(plan)
        index = self.calls
        self.calls += 1
        if self.interrupted:
            raise KeyboardInterrupt("simulated hard interruption after durable reservation")
        status, makespan, added = self.results[index]
        if status != "success":
            return {"status": status, "cache_hit": False, "returncode": 1, "error": "synthetic failure"}
        folder = search.run_dir / "fake_evidence" / str(index)
        folder.mkdir(parents=True)
        plan_file, result_file = folder / "plan.json", folder / "result.json.gz"
        plan_file.write_text(json.dumps(plan, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        raw = {"scene": "B", "problem": problem, "num_cores": search.num_cores,
               "makespan": makespan, "data_movement_bytes": {"added_copy_bytes": added},
               "test_fixture_not_official": True}
        with gzip.open(result_file, "wt", encoding="utf-8") as handle:
            json.dump(raw, handle)
        sources = search.manifest["source_sha256"]
        hashes = {"graph_sha256": search.manifest["graph_sha256"],
                  "config_sha256": search.manifest["config_sha256"],
                  "plan_sha256": refine.object_digest(plan), "problem": problem,
                  "official_py_sha256": {p.name: sources[str(p.resolve())] for p in refine.OFFICIAL.glob("*.py")},
                  "wrapper_sha256": sources[str((refine.RESEARCH / "solver/evaluator.py").resolve())],
                  "worker_sha256": sources[str((refine.RESEARCH / "solver/eval_worker.py").resolve())]}
        return {"status": "success", "problem": problem, "cache_hit": self.cache_hit,
                "returncode": 0, "hashes": hashes, "plan_path": str(plan_file),
                "result_path": str(result_file), "result_sha256": refine.digest(result_file),
                "metrics": {"makespan": makespan, "data_movement_bytes": {"added_copy_bytes": added}}}


class RefineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.graph = self.base / "graph.json"
        refine.atomic_json(self.graph, {"ops": [{"id": i, "op": "ADD", "pipe": "PIPE_V", "cycles": i}
                                                for i in range(1, 5)], "tensors": [], "edges": []})
        self.incumbent = self.base / "incumbent.json"
        refine.atomic_json(self.incumbent, plan_variant(0))

    def tearDown(self):
        self.tmp.cleanup()

    def search(self, **kwargs):
        return refine.Refiner(self.graph, 1, kwargs.pop("problem", 2), incumbent_plan=self.incumbent,
                              config=refine.DATA / "config.txt", run_dir=self.base / "run", **kwargs)

    def test_stagnation_then_duplicate_only_round_then_improvement(self):
        called = []
        def gen(ir, plan, raw, **kw):
            called.append(kw["round_index"])
            return [candidate(1 if kw["round_index"] < 2 else 2)], {"synthetic": True}
        s = self.search(trace_fn=gen, max_rounds=3)
        f = s.evaluate = FakeEvaluator(s, [("success", 100, 10), ("success", 110, 1), ("success", 90, 20)])
        out = s.run()
        self.assertEqual(called, [0, 1, 2])
        self.assertEqual(f.calls, 3)
        self.assertEqual(refine.key(out["best"]["record"]), (90, 20))
        self.assertEqual(len(out["duplicates"]), 1)
        self.assertEqual([r["improved"] for r in out["rounds"]], [False, False, True])

    def test_budget_cap_duplicates_failures_and_cache_hit_charged(self):
        def gen(ir, plan, raw, **kw):
            items = [candidate(1), candidate(1), candidate(2)] if kw["round_index"] == 0 else [candidate(3)]
            return items[:kw["max_candidates"]], {}
        s = self.search(trace_fn=gen, max_evaluations=4, round_width=4, trace_cap=8)
        f = s.evaluate = FakeEvaluator(s, [("success", 100, 10), ("execution_cycle", 0, 0),
                                          ("success", 101, 0), ("success", 100, 9)], cache_hit=True)
        out = s.run()
        self.assertEqual(f.calls, 4)
        self.assertEqual(out["logical_calls"], 4)
        self.assertEqual(out["cache_hits"], 3)
        self.assertEqual(out["status_counts"]["execution_cycle"], 1)
        self.assertEqual(refine.key(out["best"]["record"]), (100, 9))
        self.assertEqual(out["stop_reason"], "evaluation_cap")
        self.assertTrue(any(d["name"] == "v1" for d in out["duplicates"]))

    def test_initial_failure_never_emits_unverified_best(self):
        s = self.search(trace_fn=lambda *a, **k: self.fail("generator must not run"))
        s.evaluate = FakeEvaluator(s, [("timeout", 0, 0)])
        out = s.run()
        self.assertEqual(out["logical_calls"], 1)
        self.assertFalse(out["completed"])
        self.assertIsNone(out["best"])
        self.assertFalse((s.run_dir / "best.plan.json").exists())

    def test_initial_only_budget_one(self):
        s = self.search(max_evaluations=1, trace_fn=lambda *a, **k: self.fail("no budget"))
        s.evaluate = FakeEvaluator(s, [("success", 100, 10)])
        out = s.run()
        self.assertEqual(out["logical_calls"], 1)
        self.assertTrue(out["completed"])
        self.assertEqual(out["best"]["stage"], "initial")

    def test_pending_survives_interruption_before_return(self):
        s = self.search()
        s.evaluate = FakeEvaluator(s, [], interrupted=True)
        with self.assertRaises(KeyboardInterrupt):
            s.run()
        disk = refine.read_json(s.run_dir / "checkpoint.json")
        self.assertEqual(disk["logical_calls"], 1)
        self.assertEqual(disk["pending_calls"], 1)
        self.assertEqual(disk["worker_launch_unknown_calls"], 1)
        self.assertFalse(disk["completed"])
        self.assertTrue((s.run_dir / "trials/0000.json").is_file())

    def test_p3_trace_and_cache_share_current_best_and_cap(self):
        seen = []
        def trace(ir, plan, raw, **kw):
            return [candidate(1)], {}
        def cache(ir, plan, raw, **kw):
            seen.append(refine.object_digest(plan))
            return [candidate(2)], {}
        s = self.search(problem=3, trace_fn=trace, cache_fn=cache, trace_cap=1, cache_cap=1, max_rounds=5)
        s.evaluate = FakeEvaluator(s, [("success", 100, 10), ("success", 90, 10), ("success", 80, 10)])
        out = s.run()
        self.assertEqual(seen, [refine.object_digest(plan_variant(1))])
        self.assertEqual(out["stage_calls"], {"initial": 1, "trace": 1, "cache": 1})
        self.assertEqual(out["evaluations"][2]["parent_plan_sha256"], refine.object_digest(plan_variant(1)))

    def test_cache_zero_or_p2_never_calls_generator(self):
        for problem, cap in ((2, 10), (3, 0)):
            if (self.base / "run").exists():
                import shutil
                shutil.rmtree(self.base / "run")
            s = self.search(problem=problem, trace_cap=0, cache_cap=cap,
                            cache_fn=lambda *a, **k: self.fail("cache must not be called"))
            s.evaluate = FakeEvaluator(s, [("success", 100, 10)])
            self.assertEqual(s.run()["logical_calls"], 1)

    def test_generation_failure_is_recorded_and_next_round_runs(self):
        def gen(ir, plan, raw, **kw):
            if kw["round_index"] == 0:
                raise ValueError("synthetic generator failure")
            return [candidate(2)], {}
        s = self.search(trace_fn=gen, max_rounds=2)
        s.evaluate = FakeEvaluator(s, [("success", 100, 10), ("success", 80, 20)])
        out = s.run()
        self.assertEqual(len(out["generation_failures"]), 1)
        self.assertEqual(refine.key(out["best"]["record"]), (80, 20))

    def test_success_with_wrong_hash_or_nan_does_not_replace_best(self):
        s = self.search(trace_fn=lambda *a, **k: ([candidate(1)], {}), max_rounds=1)
        fake = FakeEvaluator(s, [("success", 100, 10), ("success", 80, 0)])
        def evaluate(*a, **k):
            r = fake(*a, **k)
            if fake.calls > 1:
                r["hashes"]["problem"] = 3
            return r
        s.evaluate = evaluate
        out = s.run()
        self.assertEqual(out["status_counts"]["evidence_verification_failed"], 1)
        self.assertEqual(refine.key(out["best"]["record"]), (100, 10))
        with self.assertRaises(ValueError):
            refine.key({"metrics": {"makespan": float("nan"), "data_movement_bytes": {"added_copy_bytes": 0}}})

    def test_dict_insertion_order_is_not_canonicalized(self):
        p, q = plan_variant(0), plan_variant(0)
        q["node_to_subgraph"] = dict(reversed(list(q["node_to_subgraph"].items())))
        self.assertNotEqual(refine.object_digest(p), refine.object_digest(q))

    def test_best_evidence_corruption_aborts_and_raw_bytes_must_match(self):
        def gen(ir, plan, raw, **kw):
            Path(s.best["record"]["result_path"]).write_bytes(b"changed evidence")
            return [], {}
        s = self.search(trace_fn=gen, max_rounds=2)
        s.evaluate = FakeEvaluator(s, [("success", 100, 10)])
        out = s.run()
        self.assertFalse(out["completed"])
        self.assertEqual(out["status"], "integrity_failure")
        self.assertTrue(out["best_retained_for_audit_only"])

    def test_tampered_added_bytes_rejected(self):
        s = self.search(max_evaluations=1)
        fake = FakeEvaluator(s, [("success", 100, 10)])
        def evaluate(*a, **k):
            result = fake(*a, **k)
            result["metrics"]["data_movement_bytes"]["added_copy_bytes"] = 0
            return result
        s.evaluate = evaluate
        out = s.run()
        self.assertIsNone(out["best"])
        self.assertEqual(out["status_counts"]["evidence_verification_failed"], 1)

    def test_nonfinite_failure_evidence_is_preserved_without_poisoning_json(self):
        s = self.search(max_evaluations=1)
        fake = FakeEvaluator(s, [("success", 100, 10)])
        def evaluate(*a, **k):
            result = fake(*a, **k)
            result["metrics"]["data_movement_bytes"]["added_copy_bytes"] = float("nan")
            return result
        s.evaluate = evaluate
        out = s.run()
        self.assertIsNone(out["best"])
        self.assertEqual(out["evaluations"][0]["unverified_returned_record"]["metrics"]["data_movement_bytes"]["added_copy_bytes"],
                         {"invalid_nonfinite_number": "nan"})
        self.assertTrue((s.run_dir / "summary.json").is_file())

    def test_sibling_generation_parent_is_separate_from_current_incumbent(self):
        s = self.search(trace_fn=lambda *a, **k: ([candidate(1), candidate(2)], {}), max_rounds=1)
        s.evaluate = FakeEvaluator(s, [("success", 100, 10), ("success", 90, 10), ("success", 80, 10)])
        out = s.run()
        first, second = out["evaluations"][1:]
        self.assertEqual(first["parent_plan_sha256"], second["parent_plan_sha256"])
        self.assertEqual(second["before_incumbent_sha256"], first["plan_sha256"])

    def test_paths_reject_existing_empty_dangling_and_alias_collisions(self):
        (self.base / "run").mkdir()
        with self.assertRaisesRegex(ValueError, "fresh"):
            self.search()
        (self.base / "run").rmdir()
        (self.base / "run").symlink_to(self.base / "missing")
        with self.assertRaisesRegex(ValueError, "fresh"):
            self.search()
        (self.base / "run").unlink()
        with self.assertRaisesRegex(ValueError, "aliases"):
            self.search(evaluation_dir=self.incumbent)
        with self.assertRaises(ValueError):
            refine.checked_paths(self.graph, refine.DATA / "config.txt", self.incumbent,
                                 refine.DATA / "new_refine_attempt", None)
        cache = self.base / "cache"
        cache.mkdir()
        (cache / "attempts").symlink_to(refine.DATA, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.search(evaluation_dir=cache)
        (cache / "attempts").unlink()
        (cache / "cache").mkdir()
        (cache / "cache" / "fakekey").symlink_to(refine.DATA, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.search(evaluation_dir=cache)
        with self.assertRaisesRegex(ValueError, "audit"):
            self.search(evaluation_dir=self.base / "run/trials/nested")
        self.assertFalse((self.base / "run").exists())

    def test_actual_generators_p2_p3_and_all_core_counts_structure(self):
        for n in range(1, 6):
            ir, plan, _ = trace_fixture(n)
            for problem in (2, 3):
                raw = synthetic_trace(ir, plan, n, problem)
                for r in (0, 1, 2):
                    candidates, _ = refine.generate_trace_candidates(ir, plan, raw, num_cores=n, round_index=r, max_candidates=6)
                    for c in candidates:
                        self.assertTrue(refine.validate_plan(ir, c["plan"]))
                        self.assertEqual(len(c["plan"]["core_schedules"]), n)
            cache_ir, cache_plan, raw = fifo_mock()
            cache_plan["core_schedules"].extend([] for _ in range(n - 1))
            raw["num_cores"] = n
            raw["per_core_timeline"].extend({"core_id": i, "ops": []} for i in range(1, n))
            candidates, _ = refine.generate_cache_candidates(cache_ir, cache_plan, raw, num_cores=n, max_candidates=6)
            for c in candidates:
                self.assertTrue(refine.validate_plan(cache_ir, c["plan"]))
                self.assertEqual(len(c["plan"]["core_schedules"]), n)


if __name__ == "__main__":
    unittest.main()
