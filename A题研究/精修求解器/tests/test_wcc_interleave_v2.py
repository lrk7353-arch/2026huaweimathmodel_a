"""Regression tests for preserved single-WCC priority and real interleaving."""
import copy
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE.parent / "solver"), str(Path(__file__).resolve().parent)]
from graph_ir import GraphIR
from plan import validate_plan, plan_from_component_groups
from test_wcc_interleave import branches
import wcc_interleave as v1
from wcc_interleave_v2 import generate_interleave_candidates


def per_core_orders(plan):
    by_sg = {sg: int(o) for o, sg in plan["node_to_subgraph"].items()}
    return [[by_sg[sg] for sg in seq] for seq in plan["core_schedules"]]


class InterleaveV2Tests(unittest.TestCase):
    def setUp(self):
        self.ir = GraphIR.from_graph(branches(7, fork=True))
        self.plan = plan_from_component_groups(self.ir, (((0,),), ((1, 2),), ((3, 4, 5, 6),), ()))

    def gen(self, **kwargs):
        return generate_interleave_candidates(self.ir, self.plan, num_cores=4, **kwargs)

    def test_single_wcc_and_empty_core_priority_exactly_matches_control(self):
        candidates, d = self.gen()
        self.assertEqual(d["preserved_cores"], [0, 3])
        control = per_core_orders(candidates[0]["plan"])
        for candidate in candidates:
            order = per_core_orders(candidate["plan"])
            self.assertEqual(order[0], control[0])
            self.assertEqual(order[3], [])
            self.assertTrue(candidate["metadata"]["noneligible_core_order_matches_control"])

    def test_two_wcc_core_really_interleaves_not_only_renames(self):
        graph = branches(3)
        for op in graph["ops"]:
            if op["pipe"] == "PIPE_V":
                op["cycles"] = 40
        ir = GraphIR.from_graph(graph)
        plan = plan_from_component_groups(ir, (((0,),), ((1, 2),)))
        candidates, d = generate_interleave_candidates(ir, plan, num_cores=2)
        self.assertEqual(d["eligible_cores"], [1])
        first = next(c for c in candidates if c["name"] == "interleave_v2_w2_base_earliest_start")
        control_order = per_core_orders(candidates[0]["plan"])[1]
        order = per_core_orders(first["plan"])[1]
        component_order = [ir.component_by_op[o] for o in order]
        self.assertNotEqual(order, control_order)
        self.assertNotEqual(component_order[0], component_order[1])
        self.assertEqual(component_order[0], component_order[2])

    def test_control_and_noneligible_order_identical_after_recovery(self):
        plan = copy.deepcopy(self.plan)
        plan["node_to_subgraph"] = dict(reversed(list(plan["node_to_subgraph"].items())))
        candidates, d = generate_interleave_candidates(self.ir, plan, num_cores=4)
        self.assertIn("recovery", d["control_order_source"])
        reference = per_core_orders(candidates[0]["plan"])[0]
        for c in candidates: self.assertEqual(per_core_orders(c["plan"])[0], reference)

    def test_topology_coverage_core_assignment_and_window(self):
        candidates, d = self.gen()
        base = v1._assignment(self.ir, self.plan, 4)
        for c in candidates:
            p = c["plan"]
            validate_plan(self.ir, p)
            self.assertEqual(v1._assignment(self.ir, p, 4), base)
            self.assertTrue(v1._is_topology(self.ir, list(map(int, p["node_to_subgraph"]))))
            if c["metadata"]["window"] is None: continue
            for core in d["eligible_cores"]:
                left = {x.id: len(x.nodes) for x in self.ir.components}
                active = set()
                for o in per_core_orders(p)[core]:
                    cid = self.ir.component_by_op[o]; active.add(cid)
                    self.assertLessEqual(len(active), c["metadata"]["window"])
                    left[cid] -= 1
                    if not left[cid]: active.remove(cid)

    def test_all_eligible_cores_retain_v1_plans(self):
        plan = plan_from_component_groups(self.ir, (((0, 1, 2),), ((3, 4, 5, 6),)))
        old, _ = v1.generate_interleave_candidates(self.ir, plan, num_cores=2)
        new, _ = generate_interleave_candidates(self.ir, plan, num_cores=2)
        self.assertEqual([x["plan"] for x in old], [x["plan"] for x in new])

    def test_deterministic_immutable_no_case_path_or_official_calls(self):
        before = copy.deepcopy((self.ir.graph, self.plan))
        a = self.gen(seed=29)
        self.assertEqual(a, self.gen(seed=29))
        other = GraphIR.from_graph(copy.deepcopy(self.ir.graph), "case_008.json")
        self.assertEqual(a, generate_interleave_candidates(other, self.plan, num_cores=4, seed=29))
        self.assertEqual(before, (self.ir.graph, self.plan))
        self.assertEqual(a[1]["official_calls"], 0)

    def test_bound_unique_and_control_first(self):
        for cap in (0, 1, 3, 9, 12):
            cs, _ = self.gen(max_candidates=cap)
            self.assertLessEqual(len(cs), min(cap, 9))
            self.assertEqual(len(cs), len({c["metadata"]["plan_sha256"] for c in cs}))
            if cs: self.assertEqual(cs[0]["metadata"]["strategy"], "reencode_control")

    def test_cross_core_wcc_is_explicitly_unsupported(self):
        ir = GraphIR.from_graph(branches(2))
        p = {"node_to_subgraph": {str(o): i for i, o in enumerate(ir.compute_ids)},
             "core_schedules": [[0, 2], [1, 3]]}
        cs, d = generate_interleave_candidates(ir, p, num_cores=2)
        self.assertEqual(cs, [])
        self.assertFalse(d["applicable"])
        self.assertTrue(d["split_wcc_ids"])

    def test_no_eligible_core_and_bad_inputs(self):
        ir = GraphIR.from_graph(branches(1))
        p = plan_from_component_groups(ir, (((0,),),))
        self.assertEqual(generate_interleave_candidates(ir, p, num_cores=1)[0], [])
        for kwargs in ({"num_cores": 3}, {"max_candidates": -1}, {"seed": False}):
            args = dict(num_cores=4); args.update(kwargs)
            with self.assertRaises(ValueError): generate_interleave_candidates(self.ir, self.plan, **args)


if __name__ == "__main__":
    unittest.main()
