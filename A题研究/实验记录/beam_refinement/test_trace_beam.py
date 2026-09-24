"""Synthetic search trees only; never calls the official evaluator."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import trace_beam as beam


def plan(assignment):
    schedules = [[] for _ in range(5)]
    for sg, core in enumerate(assignment):
        schedules[core].append(sg)
    return {"node_to_subgraph": {str(i + 1): i for i in range(len(assignment))}, "core_schedules": schedules}


class BeamControllerTests(unittest.TestCase):
    def setUp(self):
        self.ir = SimpleNamespace(compute_ids=(1, 2, 3, 4))
        self.plans = {"A": plan([0, 0, 0, 0]), "D": plan([0, 0, 0, 1]), "B": plan([1, 1, 0, 0]),
                      "X": plan([2, 2, 2, 2]), "F": plan([0, 0, 1, 1]), "E": plan([1, 1, 1, 0])}
        self.values = {"A": 100, "D": 99, "B": 105, "X": 130, "F": 98, "E": 90}
        self.names = {beam.object_digest(p): name for name, p in self.plans.items()}
        self.protocol = {"max_rounds": 5, "candidates_per_parent": 6, "relative_makespan_slack": .08,
                         "matched_comparison_cap": 30}
        self.initial = self.state("A")

    def record(self, name, cached=False):
        return {"status": "success", "cache_hit": cached, "metrics": {"makespan": self.values[name],
                "data_movement_bytes": {"added_copy_bytes": 0}}}

    def state(self, name):
        return beam.make_state(self.plans[name], self.record(name), candidate=name)

    def generate(self, parent, layer, maximum):
        tree = {"A": ["D", "B", "X"], "D": ["F"], "B": ["E"]}
        name = self.names[parent["key"]]
        return [{"name": n, "plan": self.plans[n], "metadata": {}} for n in tree.get(name, [])][:maximum], {"synthetic_test": True}

    def evaluate(self, candidate):
        return self.record(candidate["name"])

    def test_nonmonotonic_state_is_retained_and_really_expanded(self):
        events = []
        result = beam.search_strategy(self.ir, self.initial, width=3, logical_cap=20, protocol=self.protocol,
            generate=self.generate, evaluate_one=self.evaluate, emit=events.append)
        self.assertEqual(result["best"]["metrics"][0], 90)
        evidence = beam.ancestry(result, result["best"]["key"])
        self.assertEqual([n["candidate"] for n in evidence["path"]], ["A", "B", "E"])
        self.assertTrue(evidence["has_worsening_parent_child_edge"])
        self.assertTrue(evidence["has_ancestor_worse_than_incumbent_at_creation"])
        expansions = [e for e in events if e["event"] == "expand" and e["parent"] == beam.object_digest(self.plans["B"])]
        self.assertTrue(expansions)
        self.assertFalse(expansions[0]["parent_is_global_incumbent"])

    def test_globalbest_monotone_and_greedy_keeps_only_incumbent(self):
        before = copy.deepcopy(self.initial)
        result = beam.search_strategy(self.ir, self.initial, width=1, logical_cap=30, protocol=self.protocol,
            generate=self.generate, evaluate_one=self.evaluate)
        scores = [p["metrics"] for p in result["best_by_prefix"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(result["best"]["metrics"][0], 98)
        self.assertEqual(self.initial, before)
        self.assertTrue(all(len(layer["next_frontier"]) == 1 for layer in result["layers"]))

    def test_slack_cutoff_and_farthest_selection(self):
        selected, diagnostics = beam.select_frontier(self.ir, [self.state(n) for n in ("A", "D", "B", "X")], self.state("D"), 3, .08)
        self.assertEqual(selected[0]["candidate"], "D")
        self.assertIn("B", [s["candidate"] for s in selected])
        self.assertNotIn("X", [s["candidate"] for s in selected])
        self.assertAlmostEqual(diagnostics["threshold_makespan"], 106.92)
        rejected = next(d for d in diagnostics["decisions"] if d["key"] == beam.object_digest(self.plans["X"]))
        self.assertEqual(rejected["reason"], "outside_slack")

    def test_budget_partial_layer_and_duplicates_are_recorded(self):
        events = []
        result = beam.search_strategy(self.ir, self.initial, width=3, logical_cap=2, protocol=self.protocol,
            generate=self.generate, evaluate_one=self.evaluate, emit=events.append)
        self.assertEqual(result["logical_trials"], 2)
        self.assertEqual(result["stop_reason"], "logical_cap")
        self.assertTrue(result["layers"][-1]["partial_layer"])
        self.assertTrue(any(e["event"] == "not_evaluated" for e in events))
        full = beam.search_strategy(self.ir, self.initial, width=3, logical_cap=30, protocol=self.protocol,
            generate=self.generate, evaluate_one=self.evaluate)
        self.assertGreater(len(full["duplicates"]), 0)
        self.assertEqual(len({t["key"] for t in full["trials"]}), full["logical_trials"])

    def test_cache_hits_and_failures_still_count_logical_trials(self):
        def evaluate(candidate):
            if candidate["name"] == "X":
                return {"status": "timeout", "cache_hit": False, "metrics": {}}
            return self.record(candidate["name"], cached=True)
        result = beam.search_strategy(self.ir, self.initial, width=3, logical_cap=30, protocol=self.protocol,
            generate=self.generate, evaluate_one=evaluate)
        self.assertEqual(result["status_counts"]["timeout"], 1)
        self.assertEqual(result["logical_trials"], result["cache_hits"] + result["official_calls"])
        self.assertEqual(result["official_calls"], 1)
        self.assertNotIn(beam.object_digest(self.plans["X"]), result["nodes"])

    def test_matched_prefix_does_not_use_larger_beam_final_result(self):
        greedy = beam.search_strategy(self.ir, self.initial, width=1, logical_cap=30, protocol=self.protocol,
            generate=self.generate, evaluate_one=self.evaluate)
        broad = beam.search_strategy(self.ir, self.initial, width=3, logical_cap=66, protocol=self.protocol,
            generate=self.generate, evaluate_one=self.evaluate)
        comparison = beam.compare_results(self.initial, greedy, broad, self.protocol)
        self.assertEqual(comparison["common_logical_prefix"], 4)
        self.assertEqual(comparison["matched_result"], "tie")
        self.assertEqual(comparison["matched_beam"]["metrics"][0], 98)
        self.assertEqual(comparison["beam_final_metrics"][0], 90)

    def test_zero_budget_time_and_generation_failures_preserve_initial(self):
        result = beam.search_strategy(self.ir, self.initial, width=3, logical_cap=66, protocol=self.protocol,
            generate=self.generate, evaluate_one=self.evaluate, allowed=lambda: False)
        self.assertEqual(result["logical_trials"], 0)
        self.assertEqual(result["best"]["key"], self.initial["key"])
        comparison = beam.compare_results(self.initial, result, result, self.protocol)
        self.assertEqual(comparison["common_logical_prefix"], 0)
        self.assertIsNone(comparison["matched_result"])
        def broken(*args):
            raise ValueError("synthetic generation failure")
        result = beam.search_strategy(self.ir, self.initial, width=3, logical_cap=66, protocol=self.protocol,
            generate=broken, evaluate_one=self.evaluate)
        self.assertEqual(len(result["generation_failures"]), 5)
        self.assertEqual(result["best"]["key"], self.initial["key"])


if __name__ == "__main__":
    unittest.main()
