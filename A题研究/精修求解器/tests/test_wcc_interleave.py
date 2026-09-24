"""Mechanism invariants: exact core assignment, topology, bounded WCC windows."""
import copy
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "solver"))
from graph_ir import GraphIR
from plan import validate_plan, plan_from_component_groups
from wcc_interleave import generate_interleave_candidates, _assignment, _is_topology


def branches(count=12, fork=False, shared=False):
    graph = {"ops": [], "tensors": [], "edges": []}
    oi, ti = 1, 10000
    def op(name, pipe, cycles):
        nonlocal oi
        o = oi; oi += 1
        graph["ops"].append(dict(id=o, op=name, pipe=pipe, cycles=cycles))
        return o
    def tensor(pos, size):
        nonlocal ti
        t = ti; ti += 1
        graph["tensors"].append(dict(id=t, pos=pos, size=size))
        return t
    def edge(a, b):
        graph["edges"].append(dict(source=a, target=b))
    root = None
    for i in range(count):
        if root is None or not shared:
            d, root = tensor("DDR", 16), tensor("L1", 16)
            cp = op("COPY_IN", "PIPE_MTE2", 0)
            edge(d, cp); edge(cp, root)
        m = op("MATMUL", "PIPE_M", 10 + i % 3)
        mid = tensor("UB", 32)
        edge(root, m); edge(m, mid)
        v = op("ADD", "PIPE_V", 8 + i % 2)
        out = tensor("UB", 32)
        edge(mid, v); edge(v, out)
        if fork:
            v2 = op("MUL", "PIPE_V", 4)
            mid2 = tensor("UB", 32)
            edge(mid, v2); edge(v2, mid2)
            merge = op("MATMUL", "PIPE_M", 5)
            joined = tensor("L1", 16)
            edge(out, merge); edge(mid2, merge); edge(merge, joined)
            out = joined
        cpout = op("COPY_OUT", "PIPE_MTE3", 0)
        destination = tensor("DDR", 16)
        edge(out, cpout); edge(cpout, destination)
    return graph


def initial(ir, cores=3):
    groups = tuple((tuple(c.id for c in ir.components if c.id % cores == k),)
                   if any(c.id % cores == k for c in ir.components) else () for k in range(cores))
    return plan_from_component_groups(ir, groups)


class InterleaveTests(unittest.TestCase):
    def setUp(self):
        self.ir = GraphIR.from_graph(branches(15, fork=True, shared=True))
        self.plan = initial(self.ir)

    def generate(self, **kw):
        return generate_interleave_candidates(self.ir, self.plan, num_cores=3, **kw)

    def test_coverage_assignment_and_dag_for_every_candidate(self):
        old = _assignment(self.ir, self.plan, 3)
        candidates, diag = self.generate()
        self.assertTrue(diag["applicable"])
        self.assertGreater(len(candidates), 1)
        for candidate in candidates:
            p = candidate["plan"]
            self.assertTrue(validate_plan(self.ir, p))
            self.assertEqual(_assignment(self.ir, p, 3), old)
            self.assertTrue(_is_topology(self.ir, list(map(int, p["node_to_subgraph"]))))
            self.assertEqual(len(set(p["node_to_subgraph"].values())), len(self.ir.compute_ids))

    def test_actual_prefix_active_wcc_count_respects_window(self):
        candidates, _ = self.generate()
        assignment = _assignment(self.ir, self.plan, 3)
        for candidate in candidates:
            window = candidate["metadata"]["window"]
            if window is None:
                continue
            left = {c.id: len(c.nodes) for c in self.ir.components}
            active = [set() for _ in range(3)]
            for raw in candidate["plan"]["node_to_subgraph"]:
                o = int(raw); c = self.ir.component_by_op[o]; core = assignment[o]
                active[core].add(c)
                self.assertLessEqual(len(active[core]), window)
                left[c] -= 1
                if not left[c]: active[core].remove(c)
            self.assertTrue(all(not s for s in active))
            for proxy in candidate["metadata"]["per_core_proxy"]:
                self.assertLessEqual(proxy["peak_admitted_wccs"], window)

    def test_deterministic_no_mutation_or_evaluation(self):
        before_graph, before_plan = copy.deepcopy(self.ir.graph), copy.deepcopy(self.plan)
        a = self.generate(seed=17)
        self.assertEqual(a, self.generate(seed=17))
        self.assertEqual(self.ir.graph, before_graph)
        self.assertEqual(self.plan, before_plan)
        self.assertEqual(a[1]["official_calls"], 0)
        self.assertTrue(all(not c["metadata"]["capacity_certified"] for c in a[0]))

    def test_path_and_case_name_do_not_change_candidates(self):
        another = GraphIR.from_graph(copy.deepcopy(self.ir.graph), "case_093.json")
        self.assertEqual(self.generate(), generate_interleave_candidates(another, self.plan, num_cores=3))

    def test_cap_and_exact_duplicate_elimination(self):
        for cap in (0, 1, 2, 5, 12):
            cs, _ = self.generate(max_candidates=cap)
            self.assertLessEqual(len(cs), cap)
            self.assertLessEqual(len(cs), 9)
            self.assertEqual(len(cs), len({c["metadata"]["plan_sha256"] for c in cs}))
            if cs: self.assertEqual(cs[0]["metadata"]["strategy"], "reencode_control")

    def test_control_keeps_expressed_topological_mapping_order(self):
        cs, d = self.generate(max_candidates=1)
        self.assertEqual(list(cs[0]["plan"]["node_to_subgraph"]), list(self.plan["node_to_subgraph"]))
        self.assertFalse(d["control_claims_exact_original_step1_sequence"])

    def test_control_repairs_non_topological_mapping_without_false_claim(self):
        p = copy.deepcopy(self.plan)
        p["node_to_subgraph"] = dict(reversed(list(p["node_to_subgraph"].items())))
        cs, d = generate_interleave_candidates(self.ir, p, num_cores=3, max_candidates=1)
        self.assertTrue(_is_topology(self.ir, list(map(int, cs[0]["plan"]["node_to_subgraph"]))))
        self.assertIn("recovery", d["control_order_source"])

    def test_split_wcc_is_explicitly_unsupported(self):
        ir = GraphIR.from_graph(branches(2))
        mapping = {str(o): i for i, o in enumerate(ir.compute_ids)}
        plan = {"node_to_subgraph": mapping, "core_schedules": [[0, 2], [1, 3]]}
        validate_plan(ir, plan)
        cs, d = generate_interleave_candidates(ir, plan, num_cores=2)
        self.assertEqual(cs, [])
        self.assertFalse(d["applicable"])
        self.assertEqual(len(d["split_wcc_ids"]), 2)

    def test_single_wcc_no_opportunity(self):
        ir = GraphIR.from_graph(branches(1))
        cs, d = generate_interleave_candidates(ir, initial(ir, 1), num_cores=1)
        self.assertEqual(cs, [])
        self.assertIn("no core", d["reason"])

    def test_unused_cores_stay_empty(self):
        ir = GraphIR.from_graph(branches(4))
        plan = plan_from_component_groups(ir, ((tuple(range(4)),), (), (), (), ()))
        cs, _ = generate_interleave_candidates(ir, plan, num_cores=5)
        for c in cs: self.assertEqual(c["plan"]["core_schedules"][1:], [[], [], [], []])

    def test_phase_predictor_overlaps_independent_m_v_work(self):
        ir = GraphIR.from_graph(branches(8))
        cs, _ = generate_interleave_candidates(ir, initial(ir, 1), num_cores=1)
        for c in cs[1:]:
            p = c["metadata"]["per_core_proxy"][0]
            self.assertLess(p["compute_only_predicted_end"], sum(p["compute_work"].values()))

    def test_parameter_mismatch_rejected(self):
        for kw in ({"num_cores": 2}, {"num_cores": True}, {"max_candidates": -1}, {"seed": False}):
            args = dict(num_cores=3); args.update(kw)
            with self.assertRaises(ValueError):
                generate_interleave_candidates(self.ir, self.plan, **args)


if __name__ == "__main__":
    unittest.main()
