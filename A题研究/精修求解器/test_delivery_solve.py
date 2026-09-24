"""Delivery controller mechanism tests; no official evaluator calls."""
import copy
import gzip
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import controller as c


def variant(v, n=1):
    return {"node_to_subgraph": {str(i): 10 * v + i for i in range(1, 5)},
            "core_schedules": [[10 * v + i for i in range(1, 5)]] + [[] for _ in range(n - 1)]}


def candidate(v, n=1, **metadata):
    return {"name": "variant_" + str(v), "plan": variant(v, n), "metadata": metadata}


def empty(*args, **kwargs):
    return [], {"synthetic_empty_generator": True}


class FakeOfficial:
    """Explicit synthetic evidence, never used to establish actual performance."""
    def __init__(self, search, outcomes, interrupted=False):
        self.s, self.outcomes, self.calls, self.interrupted = search, outcomes, 0, interrupted

    def __call__(self, graph, plan, problem, run_dir, **kwargs):
        s, index = self.s, self.calls
        disk = c.read_json(s.run_dir / "checkpoint.json")
        assert disk["state"] == "evaluation_pending" and disk["logical_calls"] == index + 1
        assert disk["evaluations"][-1]["state"] == "pending"
        self.calls += 1
        if self.interrupted:
            raise KeyboardInterrupt("simulated interruption")
        status, span, added, cache = self.outcomes[index]
        if status != "success":
            return {"status": status, "cache_hit": False, "returncode": 1}
        folder = s.run_dir / "fake_records" / str(index)
        folder.mkdir(parents=True)
        pp, rp = folder / "plan.json", folder / "result.json.gz"
        pp.write_text(json.dumps(plan, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        mapping = {int(o): sg for o, sg in plan["node_to_subgraph"].items()}
        timelines = []
        for core, order in enumerate(plan["core_schedules"]):
            ops = []
            for op in s.ir.compute_ids:
                if mapping[op] in order:
                    original = s.ir.ops[op]
                    row = {"op_id": op, "op": original["op"], "pipe": original["pipe"],
                           "start": span - max(1, original["cycles"]), "end": span,
                           "task_id": mapping[op] if problem == 1 else core, "subgraph_id": mapping[op]}
                    ops.append(row)
            tasks = ([{"task_id": sg, "subgraph_id": sg} for sg in order] if problem == 1 else
                     ([{"task_id": core, "subgraph_ids": order}] if order else []))
            timelines.append({"core_id": core, "tasks": tasks, "ops": ops})
        movement = {"original_graph_copy_bytes": 1, "scheduled_copy_bytes": 1 + added, "added_copy_bytes": added}
        raw = {"scene": "A" if problem == 1 else "B", "num_cores": s.num_cores,
               "makespan": span, "bandwidth_bytes_per_cycle": 60,
               "capacity_bytes": {"L1": 524288, "UB": 131072}, "data_movement_bytes": movement,
               "input_graph": Path(graph).name, "input_plan": pp.name, "per_core_timeline": timelines,
               "test_fixture_not_official": True}
        if problem == 1:
            raw.update(task_cross_core_wait_cycles=1000, task_same_core_wait_cycles=100)
        else:
            raw.update(cross_core_copy_delay_cycles=500)
        if problem == 3:
            raw.update(problem=3, cache_mode="read_only", cache_capacity_bytes=1048576, cache_bandwidth_bytes_per_cycle=250,
                       cache_stats={"copy_in_hits": 0, "copy_in_misses": 0, "hit_bytes": 0, "miss_bytes": 0})
        with gzip.open(rp, "wt", encoding="utf-8") as out:
            json.dump(raw, out)
        src = s.manifest["source_sha256"]
        hashes = {"graph_sha256": s.manifest["graph_sha256"], "config_sha256": s.manifest["config_sha256"],
                  "plan_sha256": c.object_digest(plan), "problem": problem,
                  "official_py_sha256": {p.name: src[str(p.resolve())] for p in c.OFFICIAL.glob("*.py")},
                  "wrapper_sha256": src[str((c.RESEARCH / "solver/evaluator.py").resolve())],
                  "worker_sha256": src[str((c.RESEARCH / "solver/eval_worker.py").resolve())]}
        metrics = {"makespan": span, "num_cores": s.num_cores, "data_movement_bytes": movement}
        if problem == 3:
            metrics["cache_stats"] = raw["cache_stats"]
        return {"status": "success", "problem": problem, "returncode": 0, "cache_hit": cache,
                "hashes": hashes, "plan_path": str(pp), "result_path": str(rp), "result_sha256": c.digest(rp),
                "metrics": metrics}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.graph = self.root / "graph.json"
        c.atomic_json(self.graph, {"ops": [{"id": i, "op": "ADD", "pipe": "PIPE_M" if i % 2 else "PIPE_V", "cycles": i}
                                           for i in range(1, 5)], "tensors": [], "edges": []})

    def tearDown(self):
        self.tmp.cleanup()

    def search(self, problem=2, n=1, **kwargs):
        generators = {s: empty for s in ("component", "operation", "selective", "wcc", "trace", "cache")}
        generators.update(kwargs.pop("generators", {}))
        return c.Solver(self.graph, n, problem, config=c.DATA / "config.txt", run_dir=self.root / "run",
                        generators=generators, **kwargs)

    def test_budget_initial_cache_failure_duplicates(self):
        inc = self.root / "incumbent.json"; c.atomic_json(inc, variant(0))
        def generate(*a, **kw):
            return [candidate(0), candidate(1), candidate(2)][:kw["max_candidates"]], {}
        s = self.search(incumbent_plan=inc, generators={"component": generate,
            "operation": lambda *a, **k: ([candidate(3)], {})}, max_evaluations=4)
        s.evaluate = FakeOfficial(s, [("success", 100, 10, True), ("timeout", 0, 0, False),
                                     ("success", 110, 0, False), ("success", 120, 0, False)])
        out = s.run()
        self.assertEqual(out["logical_calls"], 4)
        self.assertEqual(out["cache_hits"], 1)
        self.assertEqual(c.objective(out["best"]["record"]), (100, 10))
        self.assertEqual(out["status_counts"]["timeout"], 1)
        self.assertTrue(out["duplicates"])

    def test_pending_survives_keyboard_interrupt(self):
        s = self.search()
        s.evaluate = FakeOfficial(s, [], interrupted=True)
        with self.assertRaises(KeyboardInterrupt):
            s.run()
        out = c.read_json(s.run_dir / "checkpoint.json")
        self.assertEqual((out["logical_calls"], out["pending_calls"]), (1, 1))
        self.assertFalse(out["completed"])

    def test_stagnation_duplicate_round_then_improvement(self):
        rounds = []
        def generate(*a, **kw):
            r = kw["round_index"]; rounds.append(r)
            return [candidate(1 if r < 2 else 2)], {}
        s = self.search(generators={"trace": generate}, max_rounds=3, wcc_cap=0)
        s.evaluate = FakeOfficial(s, [("success", 100, 10, False), ("success", 110, 0, False), ("success", 90, 10, False)])
        out = s.run()
        self.assertEqual(rounds, [0, 1, 2])
        self.assertEqual(c.objective(out["best"]["record"]), (90, 10))
        self.assertEqual(out["logical_calls"], 3)

    def test_p1_only_strictly_greater_bound_prunes(self):
        def bound(ir, plan):
            return {"value": 100 if plan == variant(1) else 101, "scope": "synthetic bound"}
        s = self.search(problem=1, generators={"selective": lambda *a, **k: ([candidate(1), candidate(2)], {})}, lower_bound_fn=bound)
        s.evaluate = FakeOfficial(s, [("success", 100, 10, False), ("success", 100, 9, False)])
        out = s.run()
        self.assertEqual(out["logical_calls"], 2)
        self.assertEqual(len(out["pruned"]), 1)
        self.assertEqual(c.objective(out["best"]["record"]), (100, 9))

    def test_p2_p3_never_use_p1_lower_bound(self):
        for problem in (2, 3):
            self.root = Path(self.tmp.name) / str(problem); self.root.mkdir()
            s = self.search(problem=problem, lower_bound_fn=lambda *a: self.fail("P1 LB wrongly applied"),
                generators={"operation": lambda *a, **k: ([candidate(1, plan_lower_bound={"value": 999999})], {})})
            s.evaluate = FakeOfficial(s, [("success", 100, 10, False), ("success", 90, 10, False)])
            self.assertEqual(s.run()["logical_calls"], 2)

    def test_current_best_used_by_cache_after_trace(self):
        seen = []
        def cache(ir, plan, raw, **kw):
            seen.append(c.object_digest(plan)); return [candidate(2)], {}
        s = self.search(problem=3, max_rounds=1, trace_cap=1, cache_cap=1,
            generators={"trace": lambda *a, **k: ([candidate(1)], {}), "cache": cache})
        s.evaluate = FakeOfficial(s, [("success", 100, 10, False), ("success", 90, 10, False), ("success", 80, 10, False)])
        out = s.run()
        self.assertEqual(seen, [c.object_digest(variant(1))])
        self.assertEqual(out["best"]["parent_plan_sha256"], seen[0])

    def test_failed_initial_can_recover_from_graph_component(self):
        inc = self.root / "inc.json"; c.atomic_json(inc, variant(9))
        s = self.search(incumbent_plan=inc, generators={"component": lambda *a, **k: ([candidate(0)], {})}, max_evaluations=2)
        s.evaluate = FakeOfficial(s, [("invalid_plan", 0, 0, False), ("success", 100, 10, False)])
        out = s.run(); self.assertTrue(out["completed"]); self.assertEqual(out["best"]["stage"], "component")

    def test_source_change_aborts_before_official_call(self):
        s = self.search(); fake = s.evaluate = FakeOfficial(s, [])
        with patch.object(c, "source_hashes", return_value={"changed": "sha"}):
            out = s.run()
        self.assertEqual(fake.calls, 0)
        self.assertFalse(out["source_and_input_hashes_verified"])
        self.assertFalse(out["completed"])

    def test_raw_bytes_scene_config_graph_or_compute_core_corruption_rejected(self):
        mutations = [lambda r: r["metrics"]["data_movement_bytes"].update(added_copy_bytes=0),
                     lambda r: r["hashes"].update(problem=1)]
        for index, mutation in enumerate(mutations):
            self.root = Path(self.tmp.name) / str(index); self.root.mkdir()
            s = self.search(max_evaluations=1); fake = FakeOfficial(s, [("success", 100, 10, False)])
            def ev(*a, **kw):
                result = fake(*a, **kw); mutation(result); return result
            s.evaluate = ev
            out = s.run()
            self.assertIsNone(out["best"])
            self.assertEqual(out["status_counts"], {"evidence_verification_failed": 1})

    def test_output_alias_freshness_and_symlink_cache_rejected(self):
        for output in (self.graph, self.root / "run/summary.json", self.root / "run/trials/plan.json",
                       self.root / "run/summary.json/plan.json", self.root / "run/best.plan.json/plan.json"):
            with self.assertRaises(ValueError):
                self.search(output=output)
        with self.assertRaises(ValueError): self.search(evaluation_dir=self.root / "run/summary.json/e")
        (self.root / "run").mkdir()
        with self.assertRaises(ValueError): self.search()
        (self.root / "run").rmdir()
        (self.root / "run").symlink_to(self.root / "missing")
        with self.assertRaises(ValueError): self.search()
        (self.root / "run").unlink()
        cache = self.root / "cache/cache"; cache.mkdir(parents=True)
        (cache / "bad-key").symlink_to(c.DATA)
        with self.assertRaises(ValueError): self.search(evaluation_dir=cache.parent)

    def test_actual_generators_and_controller_accept_all_problem_core_counts(self):
        for problem in (1, 2, 3):
            for n in range(1, 6):
                run = Path(self.tmp.name) / ("p%d_n%d" % (problem, n))
                s = c.Solver(self.graph, n, problem, config=c.DATA / "config.txt", run_dir=run, max_evaluations=2)
                s.evaluate = FakeOfficial(s, [("success", 100, 10, False), ("success", 100, 9, False)])
                out = s.run()
                self.assertEqual(out["status"], "success")
                self.assertTrue(out["completed"])
                self.assertEqual(len(out["best"]["plan"]["core_schedules"]), n)

    def test_mixed_wcc_shared_control_alternation_unique_fill_and_origins(self):
        protected = [candidate(i, strategy="reencode_control" if i == 0 else "window_interleave") for i in (0, 1, 2, 3)]
        unrestricted = [candidate(i, strategy="reencode_control" if i == 0 else "window_interleave") for i in (0, 1, 4, 5)]
        with patch.object(c, "protected_interleave", return_value=(protected, {})), patch.object(c, "unrestricted_interleave", return_value=(unrestricted, {})):
            rows, diag = c.generate_wcc_candidates(None, None, num_cores=1, max_candidates=5)
            self.assertEqual([r["plan"] for r in rows], [variant(i) for i in (0, 1, 4, 2, 5)])
            self.assertEqual(len(rows[0]["metadata"]["variant_origins"]), 2)
            self.assertEqual(len(rows[1]["metadata"]["variant_origins"]), 2)
            self.assertEqual(diag["selected"], 5)

    def test_protected_policy_does_not_invoke_unrestricted(self):
        with patch.object(c, "protected_interleave", return_value=([], {})), patch.object(c, "unrestricted_interleave", side_effect=AssertionError("wrong variant")):
            c.generate_wcc_candidates(None, None, num_cores=1, policy="protected")

    def test_actual_wcc_mixed_keeps_all_five_core_counts(self):
        ir = c.GraphIR.from_path(self.graph)
        for n in range(1, 6):
            rows, diag = c.generate_wcc_candidates(ir, c.single_active_plan(ir.graph, n), num_cores=n)
            self.assertTrue(rows)
            self.assertLessEqual(len(rows), 9)
            for row in rows:
                self.assertTrue(c.validate_plan(ir, row["plan"]))
                self.assertEqual(len(row["plan"]["core_schedules"]), n)

    def test_verify_summary_roundtrip_settings_and_corruption(self):
        # No production scores are claimed: fake records live only in this
        # TemporaryDirectory. Use the unmodified CLI constructor configuration
        # to exercise resume validation (test_hooks_used=False).
        s = c.Solver(self.graph, 1, 2, config=c.DATA / "config.txt", run_dir=self.root / "run",
                     component_cap=0, operation_cap=0, wcc_cap=0, trace_cap=0, cache_cap=0, max_evaluations=1)
        s.evaluate = FakeOfficial(s, [("success", 100, 10, False)])
        out = s.run(); path = s.run_dir / "summary.json"
        verified = c.verify_summary(path, expected_settings={"problem": 2, "num_cores": 1, "timeout": 60,
            "requested_caps": out["requested_caps"], "wcc_policy": "mixed"})
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["best"]["plan"], out["best"]["plan"])
        with self.assertRaises(c.EvidenceError): c.verify_summary(path, expected_settings={"seed": 43})
        broken = copy.deepcopy(out); broken["logical_calls"] += 1; c.atomic_json(path, broken)
        with self.assertRaises(c.EvidenceError): c.verify_summary(path)
        c.atomic_json(path, out)
        Path(out["best"]["record"]["result_path"]).write_bytes(b"changed gzip")
        with self.assertRaises(c.EvidenceError): c.verify_summary(path)

    def test_timeout_default_and_explicit(self):
        s = self.search(timeout=7)
        self.assertEqual(s.timeout, 7)
        self.assertEqual(s.manifest["requested_timeout"], 7)
        with tempfile.TemporaryDirectory() as td:
            large = Path(td) / "large.json"
            c.atomic_json(large, {"ops": [{"id": i, "op": "ADD", "pipe": "PIPE_V", "cycles": 1} for i in range(10001)],
                                  "tensors": [], "edges": []})
            big = c.Solver(large, 1, 2, config=c.DATA / "config.txt", run_dir=Path(td) / "run")
            self.assertEqual(big.timeout, 180)
            self.assertIsNone(big.manifest["requested_timeout"])

    def test_renamed_graph_exact_cache_hit_uses_bytes_not_old_basename(self):
        s = self.search(max_evaluations=1)
        fake = FakeOfficial(s, [("success", 100, 10, True)])
        def ev(*a, **kw):
            record = fake(*a, **kw)
            with gzip.open(record["result_path"], "rt") as f: raw = json.load(f)
            raw["input_graph"] = "same_bytes_before_rename.json"
            with gzip.open(record["result_path"], "wt") as f: json.dump(raw, f)
            record["result_sha256"] = c.digest(record["result_path"])
            return record
        s.evaluate = ev
        out = s.run(); self.assertEqual(out["status"], "success"); self.assertEqual(out["cache_hits"], 1)

    def test_generation_failure_preserves_best_but_requires_review(self):
        def bad(*a, **kw): raise ValueError("unexpected synthetic generator failure")
        s = self.search(generators={"operation": bad})
        s.evaluate = FakeOfficial(s, [("success", 100, 10, False)])
        out = s.run()
        self.assertTrue(out["completed"])
        self.assertEqual(out["status"], "success")
        self.assertTrue(out["requires_review"])
        self.assertEqual(len(out["generation_failures"]), 1)

    def test_nonzero_returncode_and_p3_cache_stats_tamper_rejected(self):
        for index, problem in enumerate((2, 3)):
            self.root = Path(self.tmp.name) / str(index); self.root.mkdir()
            s = self.search(problem=problem, max_evaluations=1)
            fake = FakeOfficial(s, [("success", 100, 10, False)])
            def ev(*a, **kw):
                record = fake(*a, **kw)
                if problem == 2: record["returncode"] = 123
                else: record["metrics"]["cache_stats"]["copy_in_hits"] = 999
                return record
            s.evaluate = ev
            out = s.run(); self.assertIsNone(out["best"])
            self.assertEqual(out["status_counts"]["evidence_verification_failed"], 1)


if __name__ == "__main__":
    unittest.main()
