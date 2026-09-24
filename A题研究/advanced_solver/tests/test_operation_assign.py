import copy
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from operation_assign import generate_operation_candidates, memory_priority_order
from graph_ir import GraphIR
from common import DATA
from plan import validate_plan


class OperationTests(unittest.TestCase):
    def test_all_cores_produce_complete_legal_plans_without_mutating_ir(self):
        ir = GraphIR.from_path(DATA / "case_071.json")
        before = copy.deepcopy(ir.graph)
        for n in range(1, 6):
            candidates, diagnostics = generate_operation_candidates(ir, n, seed=17)
            self.assertFalse(diagnostics["generation_failures"])
            self.assertLessEqual(len(candidates), 12)
            self.assertTrue(candidates)
            for c in candidates:
                validate_plan(ir, c["plan"])
                self.assertEqual(len(c["plan"]["core_schedules"]), n)
        self.assertEqual(ir.graph, before)

    def test_seeded_priority_is_reproducible_and_topological(self):
        ir = GraphIR.from_path(DATA / "case_044.json")
        a = memory_priority_order(ir, 17, True)
        self.assertEqual(a, memory_priority_order(ir, 17, True))
        positions = {op: i for i, op in enumerate(a)}
        self.assertEqual(set(positions), set(ir.compute_ids))
        self.assertTrue(all(positions[p] < positions[v] for v in a for p in ir.predecessors[v]))

    def test_truncation_and_one_core_priority_controls_are_explicit(self):
        ir = GraphIR.from_path(DATA / "case_071.json")
        all_c, d = generate_operation_candidates(ir, 1, seed=17)
        self.assertTrue(any(c["name"].startswith("priority_only") for c in all_c))
        first, limited = generate_operation_candidates(ir, 1, max_candidates=1, seed=17)
        self.assertEqual(first, all_c[:1])
        self.assertEqual(limited["omitted_by_budget"], [c["name"] for c in all_c[1:]])


if __name__ == "__main__":
    unittest.main()
