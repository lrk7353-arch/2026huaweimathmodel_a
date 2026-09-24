"""Semantic checks for the independent whole-component reference portfolio."""
import copy
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from advanced_solver.component_baseline import component_proxy, generate_component_candidates
from solver.common import object_digest, single_active_plan
from solver.graph_ir import GraphIR
from solver.plan import plan_from_component_groups, validate_plan


def independent_branches(workloads, sharing=None):
    """Small real bipartite graph with independent M->V branches and root sharing."""
    sharing = list(range(len(workloads))) if sharing is None else sharing
    graph = {"ops": [], "tensors": [], "edges": []}
    next_op, next_tensor, roots = 1, 100000, {}
    def tensor(pos, size):
        nonlocal next_tensor
        ident = next_tensor
        next_tensor += 1
        graph["tensors"].append({"id": ident, "pos": pos, "size": size})
        return ident
    def op(kind, pipe, cycles):
        nonlocal next_op
        ident = next_op
        next_op += 1
        graph["ops"].append({"id": ident, "op": kind, "pipe": pipe, "cycles": cycles})
        return ident
    def edge(a, b):
        graph["edges"].append({"source": a, "target": b})
    for (m, v), sharing_key in zip(workloads, sharing):
        if sharing_key not in roots:
            source, local = tensor("DDR", 128), tensor("L1", 128)
            cp = op("COPY_IN", "PIPE_MTE2", 1)
            edge(source, cp)
            edge(cp, local)
            roots[sharing_key] = local
        mul, add, copyout = op("MATMUL", "PIPE_M", m), op("ADD", "PIPE_V", v), op("COPY_OUT", "PIPE_MTE3", 1)
        middle, output, destination = tensor("UB", 64), tensor("UB", 64), tensor("DDR", 64)
        for a, b in ((roots[sharing_key], mul), (mul, middle), (middle, add), (add, output),
                     (output, copyout), (copyout, destination)):
            edge(a, b)
    return graph


class ComponentProxyTests(unittest.TestCase):
    def test_root_reads_are_per_task_for_p1_and_per_core_for_p2(self):
        ir = GraphIR.from_graph(independent_branches([(10, 2), (10, 2)], [0, 0]))
        two_tasks = plan_from_component_groups(ir, (((0,), (1,)), ()))
        p = component_proxy(ir, two_tasks)
        self.assertEqual(p["root_unique_bytes"], 128)
        self.assertEqual(p["root_read_bytes_per_core_total"], 128)
        self.assertEqual(p["root_read_bytes_per_task_total"], 256)
        self.assertEqual(p["root_replication_bytes_p1"], 128)
        self.assertEqual(p["root_replication_bytes_p2"], 0)
        two_cores = plan_from_component_groups(ir, (((0,),), ((1,),)))
        p = component_proxy(ir, two_cores)
        self.assertEqual(p["root_read_bytes_per_core_total"], 256)
        self.assertEqual(p["root_read_bytes_per_task_total"], 256)
        self.assertEqual(p["original_final_output_bytes"], 128)

    def test_mv_complement_is_not_scalar_summed_and_task_switch_is_counted(self):
        ir = GraphIR.from_graph(independent_branches([(100, 1), (1, 100)], [0, 0]))
        together = component_proxy(ir, plan_from_component_groups(ir, (((0, 1),),)))
        separate = component_proxy(ir, plan_from_component_groups(ir, (((0,), (1,)),)))
        self.assertEqual(together["core_work"][0]["work_m"], 101)
        self.assertEqual(together["core_work"][0]["work_v"], 101)
        self.assertEqual(together["p1_proxy_cycles"], 101)
        self.assertEqual(together["p2_proxy_cycles"], 101)
        self.assertEqual(separate["p1_proxy_cycles"], 300)  # 100 + 100 + one 100-cycle Task switch
        self.assertEqual(separate["p2_proxy_cycles"], 101)
        self.assertFalse(together["proxy_is_certified_lower_bound"])

    def test_proxy_rejects_a_split_component_instead_of_miscounting_it(self):
        ir = GraphIR.from_graph(independent_branches([(100, 1)]))
        a, b = ir.compute_ids
        with self.assertRaises(ValueError):
            component_proxy(ir, {"node_to_subgraph": {str(a): 0, str(b): 1},
                                 "core_schedules": [[0, 1]]})


class ComponentPortfolioTests(unittest.TestCase):
    def setUp(self):
        workloads = [(120, 2), (80, 5), (2, 110), (4, 90), (70, 30), (20, 65),
                     (66, 8), (7, 60), (42, 35), (20, 10), (15, 30), (8, 17)]
        self.ir = GraphIR.from_graph(independent_branches(workloads, [i % 3 for i in range(12)]))

    def test_deterministic_bounded_and_does_not_mutate_input(self):
        original = copy.deepcopy(self.ir.graph)
        first = generate_component_candidates(self.ir, 3, max_candidates=12, seed=17)
        second = generate_component_candidates(self.ir, 3, max_candidates=12, seed=17)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first[0]), 12)
        self.assertEqual(self.ir.graph, original)
        self.assertEqual(first[1]["official_calls"], 0)

    def test_case_path_cannot_change_candidates(self):
        self.ir.path = Path("case_001.json")
        first = generate_component_candidates(self.ir, 3, seed=17)
        self.ir.path = Path("totally_different_name_999999.json")
        second = generate_component_candidates(self.ir, 3, seed=17)
        self.assertEqual(first, second)

    def test_budget_prefix_is_stable(self):
        small, _ = generate_component_candidates(self.ir, 3, max_candidates=4, seed=17)
        large, _ = generate_component_candidates(self.ir, 3, max_candidates=12, seed=17)
        self.assertEqual(small, large[:4])

    def test_exact_coverage_no_wcc_splitting_and_no_duplicate_plans(self):
        candidates, diagnostics = generate_component_candidates(self.ir, 3, seed=17)
        signatures = set()
        for item in candidates:
            self.assertEqual(set(item), {"name", "plan", "metadata"})
            self.assertTrue(validate_plan(self.ir, item["plan"]))
            self.assertEqual(len(item["plan"]["core_schedules"]), 3)
            for component in self.ir.components:
                sgs = {item["plan"]["node_to_subgraph"][str(op)] for op in component.nodes}
                self.assertEqual(len(sgs), 1)
            signatures.add(object_digest(item["plan"]))
        self.assertEqual(len(signatures), len(candidates))
        self.assertEqual(diagnostics["selected_candidates"], len(candidates))
        self.assertIn(object_digest(single_active_plan(self.ir.graph, 3)), signatures)

    def test_multiple_starts_and_task_grouping_are_structurally_triggered(self):
        candidates, diagnostics = generate_component_candidates(self.ir, 3, seed=17)
        self.assertTrue(diagnostics["second_start_triggered"])
        self.assertEqual(diagnostics["legacy_start_seeds"], [17, 104746])
        self.assertIn("batched_components", diagnostics["selected_granularities"])
        self.assertIn("per_component", diagnostics["selected_granularities"])
        self.assertIn("per_core", diagnostics["selected_granularities"])
        self.assertTrue(any(c["metadata"]["source"] == "task_aware_component_layout" for c in candidates))
        self.assertTrue({1, 3} <= set(diagnostics["selected_active_cores"]))

    def test_single_component_stays_a_single_component(self):
        ir = GraphIR.from_graph(independent_branches([(4, 3)]))
        candidates, d = generate_component_candidates(ir, 5, seed=17)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(d["selected_active_cores"], [1])
        self.assertFalse(d["second_start_triggered"])
        self.assertEqual(candidates[0]["plan"]["core_schedules"], [[0], [], [], [], []])

    def test_no_sharing_balanced_uniform_components_do_not_trigger_extra_start(self):
        ir = GraphIR.from_graph(independent_branches([(4, 4)] * 6))
        _, d = generate_component_candidates(ir, 3, seed=17)
        self.assertEqual(d["features"]["shared_root_count"], 0)
        self.assertFalse(d["second_start_triggered"])

    def test_empty_graph_and_one_core(self):
        empty = GraphIR.from_graph({"ops": [], "tensors": [], "edges": []})
        candidates, d = generate_component_candidates(empty, 1)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["plan"], {"node_to_subgraph": {}, "core_schedules": [[]]})
        self.assertEqual(d["selected_active_cores"], [0])
        candidates, _ = generate_component_candidates(self.ir, 1)
        self.assertTrue(all(len(c["plan"]["core_schedules"]) == 1 for c in candidates))

    def test_invalid_parameters(self):
        for cores in (0, 6, True, 1.0):
            with self.assertRaises(ValueError):
                generate_component_candidates(self.ir, cores)
        for maximum in (0, -1, True, 3.0):
            with self.assertRaises(ValueError):
                generate_component_candidates(self.ir, 2, max_candidates=maximum)
        with self.assertRaises(ValueError):
            generate_component_candidates(self.ir, 2, seed=True)


if __name__ == "__main__":
    unittest.main()
