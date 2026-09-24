"""Fine-depth packing mechanism checks, zero official calls."""
from pathlib import Path
import sys
import unittest
import copy

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import p1_depth_bands as module
from test_p1_convex_regions import ir_from_pairs


class DepthBandTests(unittest.TestCase):
    def test_width_one_no_scc_merge_and_at_most_n_groups_per_layer(self):
        ir = ir_from_pairs(16, [(a, b) for a in range(1, 9) for b in range(9, 17)], [10, 20, 100, 40] * 4)
        for n in range(1, 6):
            order, groups, info = module.depth_packed_groups(ir, n, 1)
            blocks, diag = module.regions._scc_coarsen(ir, groups, order)
            self.assertEqual(diag["groups_eliminated"], 0)
            self.assertLessEqual(len(blocks), n * info["dependency_levels"])

    def test_all_widths_cores_coverage_determinism_no_input_mutation(self):
        ir = ir_from_pairs(30, [(i, i + 2) for i in range(1, 29)] + [(1, 2), (29, 30)])
        original = copy.deepcopy(ir.graph)
        for n in range(1, 6):
            first = module.generate_depth_band_candidates(ir, n)
            self.assertEqual(first, module.generate_depth_band_candidates(ir, n))
            self.assertLessEqual(len(first[0]), 4)
            for candidate in first[0]:
                self.assertTrue(module.regions.validate_plan(ir, candidate["plan"]))
                self.assertEqual(len(candidate["plan"]["core_schedules"]), n)
        self.assertEqual(original, ir.graph)

    def test_independent_wccs_may_share_one_layer_without_spurious_cycles(self):
        ir = ir_from_pairs(12, [(1, 2), (3, 4), (5, 6)])
        for c in module.generate_depth_band_candidates(ir, 3)[0]:
            self.assertTrue(module.regions.validate_plan(ir, c["plan"]))


if __name__ == "__main__":
    unittest.main()
