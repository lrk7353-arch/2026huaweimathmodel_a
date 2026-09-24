"""Independent read-only mechanism review: no official evaluator calls."""
import copy
import gzip
import importlib.util
import json
from pathlib import Path
import random
import sys
import unittest

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("reviewed_p1_selective", HERE / "p1_selective.py")
p1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p1)
from graph_ir import GraphIR
from common import digest, object_digest


def graph(costs, edges=()):
    ops = [{"id": i, "op": "ADD", "pipe": pipe, "cycles": cost} for i, pipe, cost in costs]
    tensors, expanded = [], []
    for k, (a, b) in enumerate(edges):
        tid = 10000 + k
        tensors.append({"id": tid, "size": 1, "pos": "UB"})
        expanded += [{"source": a, "target": tid}, {"source": tid, "target": b}]
    return GraphIR.from_graph({"ops": ops, "tensors": tensors, "edges": expanded})


def plan(mapping, orders):
    return {"node_to_subgraph": {str(o): s for o, s in mapping.items()}, "core_schedules": orders}


class P1SelectiveIndependentReview(unittest.TestCase):
    def test_each_pipe_work_max_not_sum_across_pipes(self):
        ir = graph([(1, "PIPE_M", 10), (2, "PIPE_M", 20), (3, "PIPE_V", 50)])
        value = p1.task_lower_bound(ir, plan({1: 0, 2: 0, 3: 0}, [[0]]))
        self.assertEqual(value["value"], 50)

    def test_direct_internal_path_can_exceed_each_pipe_work(self):
        ir = graph([(1, "PIPE_M", 40), (2, "PIPE_V", 30), (3, "PIPE_M", 10)], [(1, 2), (2, 3)])
        self.assertEqual(p1.task_lower_bound(ir, plan({1: 0, 2: 0, 3: 0}, [[0]]))["value"], 80)

    def test_cross_core_1000_same_core_adjacent_100(self):
        ir = graph([(1, "PIPE_M", 10), (2, "PIPE_V", 20)], [(1, 2)])
        self.assertEqual(p1.task_lower_bound(ir, plan({1: 0, 2: 1}, [[0], [1]]))["value"], 1030)
        self.assertEqual(p1.task_lower_bound(ir, plan({1: 0, 2: 1}, [[0, 1]]))["value"], 130)

    def test_cross_and_same_wait_constraints_take_max_not_addition(self):
        ir = graph([(1, "PIPE_M", 10), (2, "PIPE_V", 20), (3, "PIPE_M", 5)], [(1, 2)])
        # Task 2 waits for remote Task 0 +1000 and local Task 1 +100.
        value = p1.task_lower_bound(ir, plan({1: 0, 2: 2, 3: 1}, [[0], [1, 2]]))
        self.assertEqual(value["value"], 1030)
        self.assertEqual([r["incoming_wait"] for r in value["critical_task_path"]], [0, 1000])

    def test_same_core_nonadjacent_dependency_does_not_add_another_wait(self):
        ir = graph([(1, "PIPE_M", 10), (2, "PIPE_V", 5), (3, "PIPE_M", 20)], [(1, 3)])
        self.assertEqual(p1.task_lower_bound(ir, plan({1: 0, 2: 1, 3: 2}, [[0, 1, 2]]))["value"], 235)

    def test_unrelated_cross_core_tasks_have_no_cross_wait(self):
        ir = graph([(1, "PIPE_M", 10), (2, "PIPE_V", 20)])
        self.assertEqual(p1.task_lower_bound(ir, plan({1: 0, 2: 1}, [[0], [1]]))["value"], 20)

    def test_zero_original_cycles_still_take_one_cycle(self):
        ir = graph([(1, "PIPE_MTE2", 0), (2, "PIPE_MTE2", 0), (3, "PIPE_MTE3", 0)])
        self.assertEqual(p1.task_lower_bound(ir, plan({1: 0, 2: 0, 3: 0}, [[0]]))["value"], 2)

    def test_copy_chain_not_a_surviving_internal_task_path_but_kept_between_tasks(self):
        raw = {"ops": [{"id": 1, "op": "ADD", "pipe": "PIPE_M", "cycles": 10000},
                       {"id": 2, "op": "ADD", "pipe": "PIPE_V", "cycles": 10000},
                       {"id": 3, "op": "COPY_OUT", "pipe": "PIPE_MTE3", "cycles": 1},
                       {"id": 4, "op": "COPY_IN", "pipe": "PIPE_MTE2", "cycles": 1}],
               "tensors": [{"id": 10, "pos": "UB", "size": 1}, {"id": 20, "pos": "DDR", "size": 1},
                           {"id": 30, "pos": "UB", "size": 1}],
               "edges": [{"source": a, "target": b} for a, b in [(1, 10), (10, 3), (3, 20), (20, 4), (4, 30), (30, 2)]]}
        ir = GraphIR.from_graph(raw)
        self.assertEqual(ir.successors[1], (2,))
        # P1 removes original COPY ops and rebuilds boundary COPY per tensor.
        # Same-Task reconstructed graphs do not reconnect this original chain.
        self.assertEqual(p1.task_lower_bound(ir, plan({1: 0, 2: 0}, [[0]]))["value"], 10000)
        # derive_multicore_plan retains COPY-contracted inter-Task dependency.
        self.assertEqual(p1.task_lower_bound(ir, plan({1: 0, 2: 1}, [[0], [1]]))["value"], 21000)

    def test_phase_and_quantile_partitions_contract_without_cycles(self):
        rng = random.Random(20260924)
        for sample in range(12):
            costs = [(i, "PIPE_M" if i % 2 else "PIPE_V", rng.randrange(0, 200)) for i in range(1, 41)]
            edges = [(i, i + 1) for i in range(1, 40) if i % 7]
            edges += [(a, b) for a in range(1, 39) for b in range(a + 2, 41) if rng.random() < .025]
            ir = graph(costs, edges)
            order = p1.topological_order(ir, "critical_path")
            for bands in (2, 5, 10):
                for pieces in (p1._phase_blocks(ir, ir.compute_ids, order, bands),
                               p1.contiguous_blocks(ir, order, bands)):
                    blocks = p1._toposort_blocks(ir, pieces, order)
                    view = p1.block_views(ir, blocks)
                    for cores in (1, 2, 3, 4, 5):
                        candidate, proxy = p1._assign(ir, blocks, view, cores, cores, "eft")
                        self.assertTrue(p1.validate_plan(ir, candidate))
                        self.assertGreaterEqual(proxy, p1.task_lower_bound(ir, candidate)["value"])

    def test_ready_list_priority_not_forced_by_block_id(self):
        ir = graph([(1, "PIPE_V", 1), (2, "PIPE_M", 30), (3, "PIPE_V", 20)], [(2, 3)])
        blocks = [[1], [2], [3]]
        result, _ = p1._assign(ir, blocks, p1.block_views(ir, blocks), 1, 1, "eft")
        self.assertEqual(result["core_schedules"], [[1, 2, 0]])
        self.assertTrue(p1.validate_plan(ir, result))

    def test_generator_caps_determinism_coverage_and_immutability(self):
        ir = graph([(i, "PIPE_M" if i % 2 else "PIPE_V", 100 + i) for i in range(1, 26)],
                   [(i, i + 1) for i in range(1, 20)])
        before = copy.deepcopy(ir.graph)
        for n in (1, 2, 3, 4, 5):
            values, diagnostics = p1.generate_selective_candidates(ir, n, 18, 17)
            self.assertEqual((values, diagnostics), p1.generate_selective_candidates(ir, n, 18, 17))
            self.assertLessEqual(len(values), 18)
            self.assertEqual(len(values), len({object_digest(c["plan"]) for c in values}))
            self.assertEqual(values[0]["metadata"]["partition"], "whole")
            for value in values:
                self.assertTrue(p1.validate_plan(ir, value["plan"]))
                self.assertEqual(len(value["plan"]["core_schedules"]), n)
        self.assertEqual(before, ir.graph)

    def test_lower_bound_below_saved_official_p1_results_no_new_calls(self):
        found = 0
        root = HERE.parent / "advanced_solver/runs/integrated_smoke_v1"
        for n in (1, 3, 5):
            path = root / ("p1_n%d" % n) / "summary.json"
            if not path.is_file():
                continue
            summary = json.loads(path.read_text())
            record = summary["best"]["record"]
            self.assertEqual(record["problem"], 1)
            self.assertEqual(digest(record["result_path"]), record["result_sha256"])
            with gzip.open(record["result_path"], "rt") as handle:
                official = json.load(handle)
            ir = GraphIR.from_path(record["graph_path"])
            candidate = json.loads(Path(record["plan_path"]).read_text())
            self.assertLessEqual(p1.task_lower_bound(ir, candidate)["value"], official["makespan"])
            found += 1
        if not found:
            self.skipTest("saved P1 results unavailable")


if __name__ == "__main__":
    unittest.main()
