"""Boundary-counting mechanisms only; zero official evaluations."""
from pathlib import Path
import copy
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from p1_boundary_lower_bound import boundary_ddr_lower_bound
from solver.graph_ir import GraphIR


def graph(ops, tensors, edges):
    return GraphIR.from_graph({"ops": [{"id": n, "op": kind, "pipe": pipe, "cycles": 1}
        for n, kind, pipe in ops], "tensors": [{"id": n, "pos": pos, "size": size}
        for n, pos, size in tensors], "edges": [{"source": a, "target": b} for a, b in edges]})


def plan(mapping, orders):
    return {"node_to_subgraph": {str(k): v for k, v in mapping.items()}, "core_schedules": orders}


class BoundaryTests(unittest.TestCase):
    def test_fanout_writes_once_per_task_reads_once_per_receiving_task(self):
        ir = graph([(i, "ADD", "PIPE_V") for i in (1, 2, 3, 4)], [(101, "UB", 61)],
                   [(1, 101), (101, 2), (101, 3), (101, 4)])
        p = plan({1: 0, 2: 1, 3: 1, 4: 2}, [[0], [1], [2]])
        d = boundary_ddr_lower_bound(ir, p)
        self.assertEqual(d["mandatory_boundary"], dict(copy_in_count=2, copy_out_count=1,
            copy_in_bytes=122, copy_out_bytes=61, total_bytes=183, nominal_ddr_cycles=6))

    def test_roots_terminals_copy_out_and_shared_local_consumer(self):
        ir = graph([(1, "ADD", "PIPE_V"), (2, "ADD", "PIPE_V"), (9, "COPY_OUT", "PIPE_MTE3")],
            [(101, "DDR", 1), (102, "UB", 60), (103, "DDR", 60)],
            [(101, 1), (1, 102), (102, 2), (102, 9), (9, 103)])
        d = boundary_ddr_lower_bound(ir, plan({1: 0, 2: 0}, [[0]]))
        self.assertEqual(d["mandatory_boundary"]["copy_in_count"], 1)
        self.assertEqual(d["mandatory_boundary"]["copy_out_count"], 1)
        self.assertEqual(d["lower_bound"], 2)
        self.assertEqual(d["conservative_compute_cross_task_subset"]["nominal_ddr_cycles"], 0)

    def test_copy_chain_follows_original_tensor_boundaries_not_contracted_ir(self):
        ir = graph([(1, "ADD", "PIPE_V"), (2, "ADD", "PIPE_V"), (8, "COPY_OUT", "PIPE_MTE3"),
                    (9, "COPY_IN", "PIPE_MTE2")],
            [(101, "UB", 61), (102, "DDR", 61), (103, "UB", 61)],
            [(1, 101), (101, 8), (8, 102), (102, 9), (9, 103), (103, 2)])
        original = copy.deepcopy(ir.graph)
        for n in range(1, 6):
            p = plan({1: 0, 2: 1}, [[0, 1]] + [[] for _ in range(n - 1)])
            d = boundary_ddr_lower_bound(ir, p)
            self.assertEqual(d["lower_bound"], 4)
            self.assertEqual(d["conservative_compute_cross_task_subset"]["nominal_ddr_cycles"], 0)
        self.assertEqual(original, ir.graph)

    def test_zero_byte_copy_still_one_unit_and_bad_bandwidth_rejected(self):
        ir = graph([(1, "ADD", "PIPE_V"), (2, "ADD", "PIPE_V")], [(101, "UB", 0)],
                   [(1, 101), (101, 2)])
        p = plan({1: 0, 2: 1}, [[0, 1]])
        self.assertEqual(boundary_ddr_lower_bound(ir, p)["lower_bound"], 2)
        with self.assertRaises(ValueError):
            boundary_ddr_lower_bound(ir, p, bandwidth=120)


if __name__ == "__main__":
    unittest.main()
