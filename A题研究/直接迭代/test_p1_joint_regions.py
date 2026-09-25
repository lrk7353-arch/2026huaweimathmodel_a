import copy
import random
import unittest

from common_run import validate_plan, GraphIR
from test_p1_task_refine import graph
from p1_joint_regions import generate, relocate, task_order, exact_key


class JointRegionTests(unittest.TestCase):
    def test_move_can_compose_with_merge_across_original_core_boundary(self):
        ir = graph(4, [(0, 2), (1, 3)])
        plan = {'node_to_subgraph': {str(i): i for i in range(4)},
                'core_schedules': [[0, 1, 3], [2]]}
        saved = copy.deepcopy((ir.graph, plan))
        candidates, _ = generate(ir, plan)
        self.assertTrue(candidates)
        self.assertTrue(any(c['metadata']['moved_ops'] > 0 and
                            c['metadata']['tasks_after'] < 4 for c in candidates))
        for c in candidates:
            validate_plan(ir, c['plan'])
        self.assertEqual(saved, (ir.graph, plan))

    def test_relocation_retains_unmoved_order_and_all_dependencies(self):
        ir = graph(4, [(0, 1), (1, 2), (2, 3)])
        # Legal execution travels 0 -> 1 -> 0; core quotient is cyclic.
        plan = {'node_to_subgraph': {str(i): i for i in range(4)},
                'core_schedules': [[0, 2, 3], [1]]}
        self.assertEqual(relocate(ir, plan, [1], 1), plan)
        moved = relocate(ir, plan, [2], 1)
        self.assertEqual(moved['core_schedules'], [[0, 3], [1, 2]])
        validate_plan(ir, moved)

    def test_large_single_task_is_split_before_moving(self):
        ir = graph(520, [])
        plan = {'node_to_subgraph': {str(i): 0 for i in range(520)},
                'core_schedules': [[0], []]}
        candidates, diag = generate(ir, plan, max_moves=4)
        self.assertIn('split512', diag['parents'])
        self.assertTrue(any(c['metadata']['parent'] == 'split512' and
                            all(c['plan']['core_schedules']) for c in candidates))

    def test_random_dags_are_deterministic_legal_and_do_not_mutate_inputs(self):
        rng = random.Random(7301)
        for _ in range(10):
            ir = graph(24, [(a, b) for a in range(24) for b in range(a+1, 24)
                            if rng.random() < .1])
            seq = [[], [], []]
            for i in range(24):
                seq[rng.randrange(3)].append(i)
            plan = {'node_to_subgraph': {str(i): i for i in range(24)},
                    'core_schedules': seq}
            saved = copy.deepcopy((ir.graph, plan))
            first, _ = generate(ir, plan, max_moves=4)
            second, _ = generate(ir, plan, max_moves=4)
            self.assertEqual([exact_key(c['plan']) for c in first],
                             [exact_key(c['plan']) for c in second])
            for c in first:
                validate_plan(ir, c['plan'])
                self.assertGreater(c['metadata']['moved_ops'], 0)
            self.assertEqual(saved, (ir.graph, plan))

    def test_bad_target_and_empty_moves_rejected(self):
        ir = graph(2, [])
        plan = {'node_to_subgraph': {'0': 0, '1': 1}, 'core_schedules': [[0], [1]]}
        for tasks, target in [([], 0), ([99], 0), ([0], -1), ([0], True)]:
            with self.assertRaises(ValueError):
                relocate(ir, plan, tasks, target)


if __name__ == '__main__':
    unittest.main()
