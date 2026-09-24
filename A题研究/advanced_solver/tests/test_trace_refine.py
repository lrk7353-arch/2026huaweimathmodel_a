"""Mechanism tests use explicit synthetic traces; saved official traces are read-only."""
import copy
import gzip
import json
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from solver.graph_ir import GraphIR
from solver.plan import validate_plan
from solver.common import DATA
from advanced_solver.trace_refine import (generate_trace_candidates, _runs_plan, _plan_assignment,
                                          _topology, _fingerprint, _tensor_views, _routes_for)


def fixture(cores=3, fanout=True):
    ops = [{"id": i, "op": "ADD", "pipe": "PIPE_V", "cycles": w}
           for i, w in [(1, 100), (2, 80), (3, 30), (4, 10), (5, 5), (6, 3)]]
    pairs = [(1, 2), (2, 3), (3, 4)] + ([(3, 5), (3, 6)] if fanout else [(4, 5), (5, 6)])
    tensors, edges = [], []
    # One tensor shared by the final fanout's consumers.
    by_producer = {}
    for source, target in pairs:
        tid = by_producer.setdefault(source, 100 + source)
        if not any(t["id"] == tid for t in tensors):
            tensors.append({"id": tid, "size": 60, "pos": "UB"})
            edges.append({"source": source, "target": tid})
        edges.append({"source": tid, "target": target})
    ir = GraphIR.from_graph({"ops": ops, "tensors": tensors, "edges": edges})
    assignment = {1: 0, 2: 0, 3: 0, 4: min(1, cores - 1), 5: min(1, cores - 1), 6: min(2, cores - 1)}
    plan = _runs_plan(ir, _topology(ir), assignment, cores)
    return ir, plan, synthetic_trace(ir, plan, cores)


def synthetic_trace(ir, plan, cores, problem=2):
    """Test fixture only, not a measured official evaluation result."""
    assignment = _plan_assignment(ir, plan, cores)
    order = _topology(ir)
    compute = {}
    timeline = [{"core_id": c, "tasks": [{"subgraph_ids": list(plan["core_schedules"][c])}], "ops": []} for c in range(cores)]
    for rank, op in enumerate(order):
        entry = {"op_id": op, "op": ir.ops[op]["op"], "pipe": ir.ops[op]["pipe"],
                 "subgraph_id": plan["node_to_subgraph"][str(op)],
                 "start": rank * 1000, "end": rank * 1000 + ir.ops[op]["cycles"]}
        entry["duration"] = entry["end"] - entry["start"]
        compute[op] = entry
        timeline[assignment[op]]["ops"].append(entry)
    views = _tensor_views(ir)
    links, generated = [], 10000
    for tid in sorted(ir.tensors):
        for source, target in sorted(_routes_for(tid, assignment, views)):
            producer = next(o for o in views[0][tid] if assignment[o] == source)
            start = compute[producer]["end"] + 1
            outgoing = {"op_id": generated, "op": "COPY_OUT", "pipe": "PIPE_MTE3", "start": start, "end": start + 1}
            incoming = {"op_id": generated + 1, "op": "COPY_IN", "pipe": "PIPE_MTE2", "start": start + 501, "end": start + 502}
            if problem == 3:
                incoming.update({"memory_path": "L2", "cache_hit": True, "cache_tensor_id": tid})
            timeline[source]["ops"].append(outgoing)
            timeline[target]["ops"].append(incoming)
            links.append({"tensor_id": tid, "size": ir.tensors[tid]["size"], "source_core": source,
                          "target_core": target, "source_copy_out_id": generated, "target_copy_in_id": generated + 1,
                          "copy_out_end": start + 1, "copy_in_release": start + 501,
                          "copy_in_start": start + 501, "copy_in_end": start + 502})
            generated += 2
    result = {"scene": "B", "num_cores": cores, "cross_core_copy_delay_cycles": 500,
              "makespan": max(o["end"] for c in timeline for o in c["ops"]),
              "per_core_timeline": timeline, "cross_core_transfers": links}
    if problem == 3:
        result["problem"] = 3
    return result


class TraceMechanismTests(unittest.TestCase):
    def test_budget_coverage_determinism_input_immutability_all_core_counts(self):
        for n in range(1, 6):
            ir, plan, trace = fixture(n)
            before = copy.deepcopy((ir.graph, plan, trace))
            for budget in (0, 1, 12):
                candidates, diagnostics = generate_trace_candidates(ir, plan, trace, num_cores=n, max_candidates=budget)
                self.assertLessEqual(len(candidates), budget)
                self.assertEqual(len({_fingerprint(c["plan"]) for c in candidates}), len(candidates))
                for candidate in candidates:
                    self.assertEqual(set(candidate), {"name", "plan", "metadata"})
                    self.assertEqual(len(candidate["plan"]["core_schedules"]), n)
                    self.assertTrue(validate_plan(ir, candidate["plan"]))
                self.assertFalse(diagnostics["exact_critical_path_claimed"])
                self.assertEqual((candidates, diagnostics), generate_trace_candidates(ir, plan, trace, num_cores=n, max_candidates=budget))
            self.assertEqual((ir.graph, plan, trace), before)

    def test_fanout_single_consumer_move_does_not_remove_shared_route(self):
        ir, plan, trace = fixture(3)
        candidates, diagnostics = generate_trace_candidates(ir, plan, trace, num_cores=3, max_candidates=24, round_index=1)
        shared = [c for c in candidates if c["metadata"].get("strategy") == "consumer_single"
                  and len(c["metadata"]["seed_route"]["consumers"]) > 1]
        self.assertTrue(shared)
        self.assertTrue(all(not c["metadata"]["seed_route_removed"] for c in shared))
        self.assertEqual(diagnostics["trace"]["multi_consumer_routes"], 1)

    def test_short_chain_round_is_bounded_connected_and_same_source_core(self):
        ir, plan, trace = fixture(3)
        candidates, _ = generate_trace_candidates(ir, plan, trace, num_cores=3, max_candidates=24, round_index=2)
        chain = [c for c in candidates if c["metadata"].get("strategy") == "producer_chain"]
        self.assertTrue(any(len(c["metadata"]["moved_ops"]) > 1 for c in chain))
        for c in chain:
            self.assertLessEqual(len(c["metadata"]["moved_ops"]), 3)
            self.assertEqual(len({m["from_core"] for m in c["metadata"]["changes"]}), 1)

    def test_non_topological_mapping_order_gets_explicit_reencoding_control(self):
        ir, plan, trace = fixture(3)
        plan["node_to_subgraph"] = dict(reversed(list(plan["node_to_subgraph"].items())))
        candidates, diagnostics = generate_trace_candidates(ir, plan, trace, num_cores=3)
        self.assertTrue(diagnostics["order_fallback"])
        self.assertEqual(candidates[0]["metadata"]["family"], "control")
        self.assertTrue(candidates[0]["metadata"]["assignment_unchanged"])

    def test_same_core_dictionary_subgraph_order_disagreement_also_falls_back(self):
        ir = GraphIR.from_graph({"ops": [{"id": i, "op": "ADD", "pipe": "PIPE_V", "cycles": i} for i in (1, 2)],
                                "tensors": [], "edges": []})
        plan = {"node_to_subgraph": {"1": 1, "2": 0}, "core_schedules": [[0, 1]]}
        trace = synthetic_trace(ir, plan, 1)
        _, diagnostics = generate_trace_candidates(ir, plan, trace, num_cores=1)
        self.assertTrue(diagnostics["order_fallback"])

    def test_fixed_assignment_priority_can_split_single_core_into_multiple_sgs(self):
        ir, plan, trace = fixture(1)
        candidates, diagnostics = generate_trace_candidates(ir, plan, trace, num_cores=1)
        priority = [c for c in candidates if c["metadata"]["family"] == "priority"]
        self.assertTrue(priority)
        self.assertTrue(any(len(c["plan"]["core_schedules"][0]) > 1 for c in priority))
        self.assertEqual(diagnostics["trace"]["mapped_transfer_count"], 0)

    def test_migrations_preserve_existing_same_core_priority_boundaries(self):
        ir, _, _ = fixture(3)
        plan = {"node_to_subgraph": {str(op): op - 1 for op in ir.compute_ids},
                "core_schedules": [list(range(6)), [], []]}
        trace = synthetic_trace(ir, plan, 3)
        candidates, diagnostics = generate_trace_candidates(ir, plan, trace, num_cores=3)
        self.assertFalse(diagnostics["base_reencoding_changes_plan"])
        migrations = [c for c in candidates if c["metadata"]["family"] == "load"]
        self.assertTrue(migrations)
        for candidate in migrations:
            self.assertEqual(len(set(candidate["plan"]["node_to_subgraph"].values())), len(ir.compute_ids))

    def test_p3_optional_cache_fields_same_scene_and_same_release(self):
        ir, plan, p2 = fixture(3)
        p3 = synthetic_trace(ir, plan, 3, problem=3)
        c2, _ = generate_trace_candidates(ir, plan, p2, num_cores=3)
        c3, _ = generate_trace_candidates(ir, plan, p3, num_cores=3)
        self.assertEqual([c["plan"] for c in c2], [c["plan"] for c in c3])
        p3["cross_core_transfers"][0]["copy_in_release"] -= 500
        with self.assertRaises(ValueError):
            generate_trace_candidates(ir, plan, p3, num_cores=3)

    def test_corrupted_trace_is_rejected_and_unknown_tensor_is_not_guessed(self):
        ir, plan, trace = fixture(3)
        for mutation in ("size", "core", "missing_compute", "time", "duplicate_route", "missing_route"):
            bad = copy.deepcopy(trace)
            if mutation == "size": bad["cross_core_transfers"][0]["size"] += 1
            elif mutation == "core": bad["cross_core_transfers"][0]["target_core"] = 0
            elif mutation == "missing_compute": bad["per_core_timeline"][0]["ops"].pop(0)
            elif mutation == "time": bad["cross_core_transfers"][0]["copy_in_end"] += 1
            elif mutation == "duplicate_route": bad["cross_core_transfers"].append(copy.deepcopy(bad["cross_core_transfers"][0]))
            else: bad["cross_core_transfers"].pop(0)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                generate_trace_candidates(ir, plan, bad, num_cores=3)
        unknown = copy.deepcopy(trace)
        synthetic = copy.deepcopy(unknown["cross_core_transfers"][0])
        synthetic["tensor_id"] = 999999
        unknown["cross_core_transfers"].append(synthetic)
        _, diagnostics = generate_trace_candidates(ir, plan, unknown, num_cores=3)
        self.assertEqual(len(diagnostics["trace"]["skipped_transfers"]), 1)

    def test_nonfinite_times_and_old_subgraph_trace_are_rejected(self):
        ir, plan, trace = fixture(3)
        for value in (float("nan"), float("inf"), -1):
            bad = copy.deepcopy(trace)
            bad["makespan"] = value
            with self.assertRaises(ValueError):
                generate_trace_candidates(ir, plan, bad, num_cores=3)
            bad = copy.deepcopy(trace)
            bad["per_core_timeline"][0]["ops"][0]["start"] = value
            with self.assertRaises(ValueError):
                generate_trace_candidates(ir, plan, bad, num_cores=3)
        # Same compute core placement does not make an old SG timeline current.
        bad = copy.deepcopy(trace)
        bad["per_core_timeline"][0]["ops"][0]["subgraph_id"] += 99
        with self.assertRaises(ValueError):
            generate_trace_candidates(ir, plan, bad, num_cores=3)
        bad = copy.deepcopy(trace)
        bad["per_core_timeline"][0]["tasks"][0]["subgraph_ids"].append(99)
        with self.assertRaises(ValueError):
            generate_trace_candidates(ir, plan, bad, num_cores=3)

    def test_load_move_uses_idle_hardware_core_and_can_emit_swap(self):
        ir, _, _ = fixture(3)
        assignment = {1: 0, 2: 0, 3: 0, 4: 1, 5: 1, 6: 1}
        plan = _runs_plan(ir, _topology(ir), assignment, 3)
        trace = synthetic_trace(ir, plan, 3)
        candidates, _ = generate_trace_candidates(ir, plan, trace, num_cores=3, max_candidates=24)
        load = [c for c in candidates if c["metadata"]["family"] == "load"]
        self.assertTrue(any(any(m["to_core"] == 2 for m in c["metadata"]["changes"]) for c in load))
        self.assertTrue(any(c["metadata"]["strategy"] == "hot_pipe_swap" for c in load))

    def test_invalid_contract_and_p1_rejected(self):
        ir, plan, trace = fixture(3)
        for args in ({"num_cores": 0}, {"num_cores": 2}, {"num_cores": 6},
                     {"num_cores": 3, "max_candidates": -1}, {"num_cores": 3, "round_index": -1},
                     {"num_cores": 3, "seed": 0.5}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                generate_trace_candidates(ir, plan, trace, **args)
        trace["scene"] = "A"
        with self.assertRaises(ValueError):
            generate_trace_candidates(ir, plan, trace, num_cores=3)


class SavedOfficialTraceTests(unittest.TestCase):
    def test_two_saved_p2_incumbents_and_p3_trace_generate_without_official_calls(self):
        cases = []
        summary_path = ROOT / "探索/runs/critical_transfer_v1/summary.json"
        if not summary_path.exists():
            self.skipTest("saved exploratory evidence is not installed")
        for row in json.loads(summary_path.read_text())["cases"]:
            cases.append((row["case"], row["selected"]["plan"], row["selected"]["record"]["result_path"], 5))
        p3_attempt = ROOT / "探索/runs/advanced_smoke/records/evaluations/attempts/20260924T101746-1c80b95d422d45abb07135944ead52d4"
        if p3_attempt.exists():
            cases.append(("case_064", json.loads((p3_attempt / "plan.json").read_text()), p3_attempt / "official_result.json.gz", 5))
        for case, plan, result_path, cores in cases:
            ir = GraphIR.from_path(DATA / (case + ".json"))
            with gzip.open(result_path, "rt", encoding="utf-8") as handle:
                official = json.load(handle)
            candidates, diagnostics = generate_trace_candidates(ir, plan, official, num_cores=cores)
            self.assertGreater(len(candidates), 0)
            self.assertEqual(diagnostics["trace"]["raw_transfer_count"], diagnostics["trace"]["mapped_transfer_count"])
            for candidate in candidates:
                self.assertTrue(validate_plan(ir, candidate["plan"]))


if __name__ == "__main__":
    unittest.main()
