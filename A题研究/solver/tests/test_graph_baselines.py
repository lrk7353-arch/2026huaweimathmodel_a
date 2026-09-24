"""Mechanism tests: dependency semantics, legality, packing and bounded search."""
import copy
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from solver.graph_ir import GraphIR
from solver.plan import validate_plan
from solver.baselines import generate_candidates, _State, _improve


def branches(workloads, sharing=None):
    """Independent M->V branches; equal sharing keys reuse one input COPY."""
    sharing = list(range(len(workloads))) if sharing is None else sharing
    graph = {"ops": [], "tensors": [], "edges": []}
    next_op, next_tensor = 1, 1000000
    copies = {}
    def tensor(pos, size=64):
        nonlocal next_tensor
        tid = next_tensor
        next_tensor += 1
        graph["tensors"].append({"id": tid, "pos": pos, "size": size})
        return tid
    def op(kind, pipe, cycles):
        nonlocal next_op
        oid = next_op
        next_op += 1
        graph["ops"].append({"id": oid, "op": kind, "pipe": pipe, "cycles": cycles})
        return oid
    def edge(a, b):
        graph["edges"].append({"source": a, "target": b})
    for (wm, wv), key in zip(workloads, sharing):
        if key not in copies:
            root, local = tensor("DDR", 128), tensor("L1", 128)
            ci = op("COPY_IN", "PIPE_MTE2", 0)
            edge(root, ci)
            edge(ci, local)
            copies[key] = local
        m, v = op("MATMUL", "PIPE_M", wm), op("ADD", "PIPE_V", wv)
        mid, output, final = tensor("UB"), tensor("UB"), tensor("DDR")
        co = op("COPY_OUT", "PIPE_MTE3", 0)
        for a, b in ((copies[key], m), (m, mid), (mid, v), (v, output), (output, co), (co, final)):
            edge(a, b)
    return graph


def dependency_graph(edges, nodes):
    graph = {"ops": [{"id": i, "op": "ADD", "pipe": "PIPE_V", "cycles": 1} for i in nodes],
             "tensors": [], "edges": []}
    for t, (a, b) in enumerate(edges, 10000):
        graph["tensors"].append({"id": t, "pos": "UB", "size": 8})
        graph["edges"].extend(({"source": a, "target": t}, {"source": t, "target": b}))
    return graph


class GraphTests(unittest.TestCase):
    def test_shared_input_is_not_compute_dependency(self):
        graph = branches([(10, 2), (20, 3)], ["shared", "shared"])
        original = copy.deepcopy(graph)
        ir = GraphIR.from_graph(graph)
        self.assertEqual(len(ir.components), 2)
        self.assertEqual(len(ir.input_sizes), 1)
        self.assertEqual(next(iter(ir.input_components.values())), (0, 1))
        self.assertEqual(ir.total_work_m, 30)
        self.assertEqual(ir.total_work_v, 5)
        self.assertEqual(graph, original)

    def test_copy_chain_preserves_compute_dependence(self):
        graph = dependency_graph([], [1, 4])
        graph["ops"].extend([
            {"id": 2, "op": "COPY_OUT", "pipe": "PIPE_MTE3", "cycles": 0},
            {"id": 3, "op": "COPY_IN", "pipe": "PIPE_MTE2", "cycles": 0}])
        graph["tensors"] = [{"id": 101, "pos": "UB", "size": 8},
                            {"id": 102, "pos": "DDR", "size": 8},
                            {"id": 103, "pos": "UB", "size": 8}]
        graph["edges"] = [{"source": a, "target": b} for a, b in
                          ((1, 101), (101, 2), (2, 102), (102, 3), (3, 103), (103, 4))]
        ir = GraphIR.from_graph(graph)
        self.assertEqual(ir.successors[1], (4,))
        self.assertEqual(len(ir.components), 1)
        self.assertFalse(ir.input_sizes)  # Intermediate DDR is not original input.

    def test_from_path_and_invalid_graph(self):
        graph = branches([(10, 2)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "case.json"
            path.write_text(json.dumps(graph), encoding="utf-8")
            ir = GraphIR.from_path(path)
            self.assertEqual(ir.path, path.resolve())
            self.assertEqual(ir.graph, graph)
        with self.assertRaises(ValueError):
            GraphIR.from_graph(dependency_graph([(1, 2), (2, 1)], [1, 2]))
        bad = copy.deepcopy(graph)
        bad["tensors"][0]["id"] = graph["ops"][0]["id"]
        with self.assertRaises(ValueError):
            GraphIR.from_graph(bad)

    def test_duplicate_json_keys_and_nonstandard_numbers_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for text in ('{"ops":[],"ops":[],"tensors":[],"edges":[]}',
                         '{"ops":[],"tensors":[],"edges":[],"extra":NaN}'):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    GraphIR.from_path(path)

    def test_original_input_must_be_bipartite(self):
        graph = dependency_graph([], [1, 2])
        graph["edges"] = [{"source": 1, "target": 2}]
        with self.assertRaises(ValueError):
            GraphIR.from_graph(graph)


class PlanTests(unittest.TestCase):
    def test_illegal_contraction_cycle(self):
        ir = GraphIR.from_graph(dependency_graph([(1, 2), (2, 3)], [1, 2, 3]))
        with self.assertRaises(ValueError):
            validate_plan(ir, {"node_to_subgraph": {"1": 0, "2": 1, "3": 0},
                               "core_schedules": [[0], [1]]})

    def test_combined_core_order_cycle(self):
        ir = GraphIR.from_graph(dependency_graph([(1, 2), (3, 4)], [1, 2, 3, 4]))
        with self.assertRaises(ValueError):
            validate_plan(ir, {"node_to_subgraph": {str(i): i for i in range(1, 5)},
                               "core_schedules": [[2, 3], [4, 1]]})

    def test_bad_coverage_bool_and_duplicate_integer_key(self):
        ir = GraphIR.from_graph(dependency_graph([], [1]))
        for mapping in ({}, {"1": True}, {"1": 0, "01": 0}, {"2": 0}):
            with self.assertRaises(ValueError):
                validate_plan(ir, {"node_to_subgraph": mapping, "core_schedules": [[0]]})


class PackingTests(unittest.TestCase):
    def setUp(self):
        self.ir = GraphIR.from_graph(branches([(120, 2), (90, 5), (2, 100), (4, 80),
                                               (40, 30), (20, 45)], [0, 0, 1, 1, 2, 2]))

    def test_contract_determinism_core_coverage_and_two_granularities(self):
        first = generate_candidates(self.ir, 3, "simple", 7)
        self.assertEqual(first, generate_candidates(self.ir, 3, "simple", 7))
        self.assertEqual({r["metadata"]["active_cores"] for r in first}, {1, 2, 3})
        self.assertEqual({r["metadata"]["granularity"] for r in first}, {"per_core", "per_component"})
        fingerprints = set()
        for record in first:
            self.assertEqual(set(record), {"name", "plan", "metadata"})
            plan = record["plan"]
            self.assertTrue(validate_plan(self.ir, plan))
            self.assertEqual(len(plan["core_schedules"]), 3)
            self.assertEqual(sum(bool(s) for s in plan["core_schedules"]), record["metadata"]["active_cores"])
            for comp in self.ir.components:
                self.assertEqual(len({plan["node_to_subgraph"][str(i)] for i in comp.nodes}), 1)
            fingerprints.add(json.dumps(plan, sort_keys=True))
        self.assertEqual(len(fingerprints), len(first))

    def test_affinity_includes_simple_and_is_deterministic(self):
        simple = generate_candidates(self.ir, 3, "simple", 5)
        affinity = generate_candidates(self.ir, 3, "affinity", 5)
        encode = lambda records: {json.dumps(r["plan"], sort_keys=True) for r in records}
        self.assertTrue(encode(simple) <= encode(affinity))
        self.assertEqual(affinity, generate_candidates(self.ir, 3, "affinity", 5))
        for record in affinity:
            self.assertTrue(validate_plan(self.ir, record["plan"]))
            metadata = record["metadata"]
            if metadata["source"] == "affinity":
                self.assertLessEqual(metadata["final_proxy"], metadata["initial_proxy"] + 1e-9)
                self.assertLessEqual(metadata["move_trials"], 2 * 64 * 2)
                self.assertLessEqual(metadata["swap_trials"], 2 * 96)

    def test_single_component_degenerates_to_one_active_core_and_deduplicates(self):
        ir = GraphIR.from_graph(branches([(5, 4)]))
        result = generate_candidates(ir, 5, "affinity")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["plan"]["core_schedules"], [[0], [], [], [], []])
        self.assertTrue(result[0]["metadata"]["also_generated_as"])

    def test_input_reads_are_deduplicated_within_core(self):
        ir = GraphIR.from_graph(branches([(5, 4), (6, 3)], [0, 0]))
        self.assertEqual(_State(ir, [0, 0], 1, 0).reads, 128)
        state = _State(ir, [0, 1], 2, 0)
        self.assertEqual(state.reads, 256)
        state.move(0, 1)
        self.assertEqual(state.reads, 128)
        state.move(0, 0)
        self.assertEqual(state.reads, 256)

    def test_swap_can_fix_pipe_imbalance_when_single_move_cannot(self):
        ir = GraphIR.from_graph(branches([(100, 1), (100, 1), (1, 100), (1, 100)]))
        assignment, stats = _improve(ir, [0, 0, 1, 1], 2, 0.0, random.Random(0))
        self.assertGreater(stats["accepted_swaps"], 0)
        self.assertEqual(stats["initial_proxy"], 200)
        self.assertEqual(stats["final_proxy"], 101)
        self.assertEqual(set(assignment), {0, 1})

    def test_unsupported_inputs(self):
        for cores in (0, -1, True, 2.0):
            with self.assertRaises(ValueError):
                generate_candidates(self.ir, cores)
        with self.assertRaises(ValueError):
            generate_candidates(self.ir, 2, "unknown")


if __name__ == "__main__":
    unittest.main()
