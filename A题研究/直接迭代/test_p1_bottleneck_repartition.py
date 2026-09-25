import copy
import time
import unittest
from collections import defaultdict
from test_p1_task_refine import graph
from common_run import validate_plan
from p1_selective import topological_order
from p1_bottleneck_repartition import partition_region, prepare, schedule, merge_region, generate


class RepartitionTests(unittest.TestCase):
    def test_fork_branches_are_separate_and_can_cross_old_boundaries(self):
        ir = graph(6, [(0, 3), (3, 4), (1, 2), (2, 5)])
        old = {'node_to_subgraph': {str(o): o // 2 for o in range(6)}, 'core_schedules': [[0, 1, 2], []]}
        saved = copy.deepcopy((ir.graph, old))
        pool, _ = generate(ir, old, [[0, 1, 2]])
        branch = [c for c in pool if c['metadata']['family'] == 'branch']
        self.assertTrue(any(c['metadata']['cross_old_boundary_blocks'] for c in branch))
        self.assertTrue(any(all(c['plan']['core_schedules']) for c in branch))
        for c in pool: validate_plan(ir, c['plan'])
        self.assertEqual((ir.graph, old), saved)

    def test_region_leave_and_reenter_cycle_rejected(self):
        ir = graph(3, [(0, 1), (1, 2)])
        old = {'node_to_subgraph': {str(i): i for i in range(3)}, 'core_schedules': [[0, 1, 2], []]}
        with self.assertRaises(ValueError):
            prepare(ir, old, [0, 2], [[0, 2]], [0, 1, 2])

    def test_outside_owner_order_survives_and_merge_protects_outside(self):
        ir = graph(6, [(0, 2), (1, 3), (2, 4), (3, 5)])
        old = {'node_to_subgraph': {str(i): i for i in range(6)}, 'core_schedules': [[0, 2, 4], [1, 3, 5]]}
        order = topological_order(ir, 'stable_id')
        blocks, bv, fixed, preds = prepare(ir, old, [2, 3], [[2], [3]], order)
        plan, _ = schedule(ir, old, blocks, bv, fixed, preds, 4)
        merged = merge_region(ir, plan, {0, 1, 4, 5}, 99999)
        for core, ops in [(0, [0, 4]), (1, [1, 5])]:
            ids = [merged['node_to_subgraph'][str(o)] for o in ops]
            self.assertLess(merged['core_schedules'][core].index(ids[0]), merged['core_schedules'][core].index(ids[1]))
        for op in (0, 1, 4, 5):
            tid = merged['node_to_subgraph'][str(op)]
            self.assertEqual(sum(t == tid for t in merged['node_to_subgraph'].values()), 1)

    def test_same_core_contraction_cannot_close_cross_core_cycle(self):
        ir = graph(3, [(0, 1), (1, 2)])
        plan = {'node_to_subgraph': {str(i): i for i in range(3)}, 'core_schedules': [[0, 2], [1]]}
        self.assertEqual(merge_region(ir, plan, set(), 999999), plan)

    def test_filename_independence_and_deadline(self):
        ir = graph(6, [(0, 2), (1, 3), (2, 4), (3, 5)])
        old = {'node_to_subgraph': {str(i): 0 for i in range(6)}, 'core_schedules': [[0], []]}
        first, _ = generate(ir, old, [[0]])
        ir.path = 'completely_unseen_graph.json'
        second, _ = generate(ir, old, [[0]])
        self.assertEqual(first, second)
        pool, diag = generate(ir, old, [[0]], seconds=-1)
        self.assertFalse(pool)
        self.assertTrue(diag['rejected'])


if __name__ == '__main__':
    unittest.main()
