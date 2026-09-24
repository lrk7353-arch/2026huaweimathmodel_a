"""Rapid-controller mechanisms using explicit fake evidence; zero official calls."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import quick_refine as q
from test_delivery_solve import FakeOfficial, candidate, variant


class Clock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


class QuickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.graph = self.root / "graph.json"
        q.frozen.atomic_json(self.graph, {"ops": [{"id": i, "op": "ADD", "pipe": "PIPE_V", "cycles": i}
            for i in range(1, 5)], "tensors": [], "edges": []})
        self.inc = self.root / "incumbent.json"
        q.frozen.atomic_json(self.inc, variant(0))
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def search(self, **kwargs):
        cores = kwargs.pop("num_cores", 1)
        q.frozen.atomic_json(self.inc, variant(0, cores))
        return q.QuickRefiner(self.graph, cores, kwargs.pop("problem", 2),
            incumbent_plan=self.inc, config=q.frozen.DATA / "config.txt",
            run_dir=kwargs.pop("run_dir", self.root / "run"), clock_fn=self.clock, **kwargs)

    def fake(self, search, outcomes, elapsed=None, interrupted=False):
        worker = FakeOfficial(search, outcomes, interrupted=interrupted)
        def call(*args, **kwargs):
            result = worker(*args, **kwargs)
            if elapsed:
                self.clock.advance(elapsed[worker.calls - 1])
            return result
        search.evaluate = call
        return worker

    def test_initial_failure_stops_without_fallback_or_generation(self):
        s = self.search(trace_fn=lambda *a, **k: self.fail("no generation after failed initial"))
        fake = self.fake(s, [("timeout", 0, 0, False)])
        out = s.run()
        self.assertEqual(fake.calls, 1)
        self.assertEqual(out["stage_calls"], {"initial": 1})
        self.assertEqual(out["stop_reason"], "initial_not_feasible")
        self.assertFalse(out["profile_completed"])
        self.assertFalse(out["best_official_verified"])
        self.assertFalse((s.run_dir / "best.plan.json").exists())

    def test_deadline_between_candidates_keeps_verified_improvement(self):
        s = self.search(time_budget=5, trace_fn=lambda *a, **k: ([candidate(1), candidate(2)], {}))
        fake = self.fake(s, [("success", 100, 10, True), ("success", 90, 12, False)], elapsed=[1, 5])
        out = s.run()
        self.assertEqual(fake.calls, 2)
        self.assertEqual(out["stop_reason"], "soft_time_budget")
        self.assertFalse(out["completed"])
        self.assertTrue(out["partial_result"] and out["best_official_verified"])
        self.assertEqual(out["soft_budget_overrun_seconds"], 1)
        self.assertEqual(out["logical_calls"], 2)
        self.assertEqual(out["pending_calls"], 0)
        self.assertEqual(q.frozen.objective(out["best"]["record"]), (90, 12))
        self.assertEqual(q.frozen.read_json(out["output"]), variant(1))
        self.assertFalse(out["stages"][0]["completed"])

    def test_generation_overrun_does_not_launch_any_new_candidate(self):
        def generate(*a, **k):
            self.clock.advance(7)
            return [candidate(1)], {"fake": True}
        s = self.search(time_budget=5, trace_fn=generate)
        fake = self.fake(s, [("success", 100, 10, False)], elapsed=[1])
        out = s.run()
        self.assertEqual(fake.calls, 1)
        self.assertEqual(out["deadline_boundary"], "after_generation:trace")
        self.assertTrue(out["partial_result"])
        self.assertTrue(Path(out["stages"][0]["generation_path"]).is_file())
        self.assertEqual(q.frozen.objective(out["best"]["record"]), (100, 10))

    def test_time_already_expired_before_run_charges_nothing(self):
        s = self.search(time_budget=1)
        s.evaluate = lambda *a, **k: self.fail("expired budget must not evaluate")
        self.clock.advance(2)
        out = s.run()
        self.assertEqual(out["logical_calls"], 0)
        self.assertEqual(out["stop_reason"], "soft_time_budget")
        self.assertFalse(out["completed"])

    def test_cache_failure_and_duplicate_accounting_preserves_best(self):
        def generate(*a, **kw):
            values = ([candidate(1), candidate(1), candidate(2)] if kw["round_index"] == 0 else [candidate(3)])
            return values[:kw["max_candidates"]], {}
        s = self.search(trace_fn=generate, max_evaluations=4)
        fake = self.fake(s, [("success", 100, 10, True), ("execution_cycle", 0, 0, False),
                             ("success", 110, 0, True), ("success", 100, 9, False)])
        out = s.run()
        self.assertEqual(fake.calls, 4)
        self.assertEqual(out["cache_hits"], 2)
        self.assertEqual(out["status_counts"]["execution_cycle"], 1)
        self.assertTrue(out["duplicates"])
        self.assertEqual(out["stop_reason"], "evaluation_cap")
        self.assertTrue(out["profile_completed"])
        self.assertFalse(out["configured_matrix_completed"])
        self.assertEqual(q.frozen.objective(out["best"]["record"]), (100, 9))

    def test_stagnant_round_does_not_stop_next_rotated_round(self):
        observed = []
        def generate(*a, **kw):
            observed.append(kw["round_index"])
            return [candidate(1 + kw["round_index"])], {}
        s = self.search(trace_fn=generate, max_rounds=2)
        self.fake(s, [("success", 100, 10, False), ("success", 120, 1, False), ("success", 90, 10, False)])
        out = s.run()
        self.assertEqual(observed, [0, 1])
        self.assertEqual(q.frozen.objective(out["best"]["record"]), (90, 10))

    def test_generation_failure_is_not_completed_even_if_later_round_improves(self):
        def generate(*a, **kw):
            if kw["round_index"] == 0:
                raise ValueError("synthetic generation failure")
            return [candidate(1)], {}
        s = self.search(trace_fn=generate)
        self.fake(s, [("success", 100, 10, False), ("success", 90, 10, False)])
        out = s.run()
        self.assertFalse(out["completed"])
        self.assertFalse(out["profile_completed"])
        self.assertTrue(out["partial_result"] and out["requires_review"])
        self.assertTrue(out["best_official_verified"])
        self.assertEqual(out["stop_reason"], "generation_failure")
        self.assertEqual(out["search_stop_reason"], "round_limit")
        self.assertEqual([r["completed"] for r in out["stages"]], [False, True])
        self.assertEqual(q.frozen.objective(out["best"]["record"]), (90, 10))

    def test_structurally_rejected_candidate_is_not_completed_or_charged(self):
        bad = candidate(1)
        del bad["plan"]["node_to_subgraph"]["4"]
        s = self.search(max_rounds=1, trace_fn=lambda *a, **k: ([bad, candidate(2)], {}))
        self.fake(s, [("success", 100, 10, False), ("success", 90, 10, False)])
        out = s.run()
        self.assertFalse(out["completed"])
        self.assertFalse(out["profile_completed"])
        self.assertFalse(out["stages"][0]["completed"])
        self.assertEqual(out["stages"][0]["rejected_candidate_count"], 1)
        self.assertEqual(out["logical_calls"], 2)
        self.assertEqual(out["stop_reason"], "candidate_rejected")
        self.assertTrue(out["requires_review"] and out["partial_result"] and out["best_official_verified"])

    def test_initial_pending_survives_interrupt(self):
        s = self.search()
        self.fake(s, [], interrupted=True)
        with self.assertRaises(KeyboardInterrupt):
            s.run()
        disk = q.frozen.read_json(s.run_dir / "checkpoint.json")
        self.assertEqual((disk["logical_calls"], disk["pending_calls"]), (1, 1))
        self.assertFalse(disk["completed"])
        self.assertEqual(q.frozen.read_json(s.run_dir / "trials/0000.json")["state"], "pending")

    def test_p3_uses_new_trace_best_then_cache_for_1_to_5_cores(self):
        for cores in range(1, 6):
            seen = []
            def cache(ir, plan, raw, **kw):
                seen.append(q.frozen.object_digest(plan))
                return [candidate(2, cores)], {}
            s = self.search(problem=3, num_cores=cores, trace_cap=1, cache_cap=1,
                run_dir=self.root / ("n" + str(cores)),
                trace_fn=lambda *a, **k: ([candidate(1, cores)], {}), cache_fn=cache)
            self.fake(s, [("success", 100, 10, False), ("success", 90, 10, False), ("success", 80, 10, False)])
            out = s.run()
            self.assertEqual(seen, [q.frozen.object_digest(variant(1, cores))])
            self.assertEqual(out["stage_calls"], {"initial": 1, "trace": 1, "cache": 1})
            self.assertEqual(len(out["best"]["plan"]["core_schedules"]), cores)

    def test_source_and_input_changes_reject_best_as_current_output(self):
        s = self.search(max_evaluations=1)
        original = q.source_hashes()
        fake = self.fake(s, [("success", 100, 10, False)])
        def changed_sources():
            result = dict(original)
            if fake.calls:
                result[str(Path(q.__file__).resolve())] = "changed"
            return result
        with patch.object(q, "source_hashes", changed_sources):
            out = s.run()
        self.assertEqual(out["status"], "integrity_failure")
        self.assertFalse(out["best_official_verified"])
        self.assertFalse(out["completed"])

    def test_raw_metrics_corruption_cannot_replace_best(self):
        s = self.search(max_rounds=1, trace_fn=lambda *a, **k: ([candidate(1)], {}))
        fake = FakeOfficial(s, [("success", 100, 10, False), ("success", 80, 10, False)])
        def call(*a, **kw):
            record = fake(*a, **kw)
            if fake.calls == 2:
                record["metrics"]["data_movement_bytes"]["scheduled_copy_bytes"] = 0
            return record
        s.evaluate = call
        out = s.run()
        self.assertEqual(out["status_counts"]["evidence_verification_failed"], 1)
        self.assertEqual(q.frozen.objective(out["best"]["record"]), (100, 10))

    def test_default_caps_and_arguments_are_explicit(self):
        for problem, caps in ((2, (8, 0)), (3, (4, 4))):
            s = self.search(problem=problem, run_dir=self.root / ("p" + str(problem)))
            self.assertEqual((s.caps["trace"], s.caps["cache"]), caps)
            self.assertEqual((s.max_evaluations, s.round_width, s.max_rounds, s.timeout), (9, 4, 2, 30))
            self.assertIn(str(Path(q.__file__).resolve()), s.manifest["source_sha256"])
        with self.assertRaises(ValueError):
            self.search(timeout=61)
        with self.assertRaises(ValueError):
            self.search(problem=1)
        with self.assertRaises(ValueError):
            self.search(time_budget=0)


if __name__ == "__main__":
    unittest.main()
