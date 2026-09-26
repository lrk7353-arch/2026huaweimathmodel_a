import time
import unittest
from common_run import GraphIR, validate_plan
from p1_joint_frontier import (_raw_model, plan_from_assignment, score_plan,
                               solve_beam, model_with_blocks, build_problem)


def graph(cycles, links=()):
    ops = [dict(id=i, op='ADD', pipe='PIPE_V', cycles=n) for i, n in enumerate(cycles)]
    tensors, edges = [], []
    for i, (a, b, size) in enumerate(links, len(ops)):
        tensors.append(dict(id=i, size=size, pos='UB'))
        edges += [dict(source=a, target=i), dict(source=i, target=b)]
    return GraphIR.from_graph(dict(ops=ops, tensors=tensors, edges=edges))


def plan(groups, owners, ncores=2):
    schedules = [[] for _ in range(ncores)]
    for b, c in enumerate(owners): schedules[c].append(b)
    return dict(node_to_subgraph={str(o): b for b, group in enumerate(groups) for o in group}, core_schedules=schedules)


class FrontierTest(unittest.TestCase):
    def test_full_plan_recomputes_merge_duration(self):
        ir = graph([100, 100], [(0, 1, 600)])
        separate = plan([[0], [1]], [0, 0])
        merged = plan([[0, 1]], [0])
        a, b = score_plan(ir, separate), score_plan(ir, merged)
        self.assertEqual(a['proxy'], 320)
        self.assertEqual(b['proxy'], 200)
        self.assertEqual(a['components']['estimated_copy_bytes'], 1200)
        self.assertEqual(b['components']['estimated_copy_bytes'], 0)

    def test_cross_release_uses_max_not_sum(self):
        ir = graph([100, 100], [(0, 1, 0)])
        self.assertEqual(score_plan(ir, plan([[0], [1]], [0, 1]))['proxy'], 1200)
        self.assertEqual(score_plan(ir, plan([[0], [1]], [0, 0]))['proxy'], 300)

    def test_zero_work_first_task_still_charges_switch(self):
        ir = graph([0, 0])
        self.assertEqual(score_plan(ir, plan([[0], [1]], [0, 0]))['proxy'], 102)

    def test_cycles_and_duplicate_ops_rejected(self):
        ir = graph([5, 5, 5], [(0, 1, 0), (1, 2, 0)])
        base = plan([[0], [1], [2]], [0, 0, 0])
        with self.assertRaises(ValueError): _raw_model(ir, base, [[0, 2], [1]], {}, [0, 0])
        with self.assertRaises(ValueError): _raw_model(ir, base, [[0], [1, 2, 2]], {}, [0, 0])

    def test_ready_task_branching_and_all_tasks_covered(self):
        ir = graph([100, 300, 100, 300], [(0, 2, 0), (1, 3, 0)])
        base = plan([[0], [1], [2], [3]], [0, 1, 0, 1])
        model = _raw_model(ir, base, [[0], [1], [2], [3]], {}, [0, 1, 0, 1])
        result, diag = solve_beam(ir, model, width=8, deadline=time.perf_counter() + 5)
        validate_plan(ir, result)
        self.assertGreater(diag['ready_task_branch_steps'], 0)
        self.assertEqual(score_plan(ir, result)['proxy'], diag['proxy'])
        self.assertEqual(sorted(diag['order']), list(range(4)))

    def test_expired_deadline_returns_complete_fallback(self):
        ir = graph([20, 30, 40])
        base = plan([[0], [1], [2]], [0, 1, 0])
        model = _raw_model(ir, base, [[0], [1], [2]], {}, [0, 1, 0])
        result, diag = solve_beam(ir, model, deadline=0)
        validate_plan(ir, result)
        self.assertTrue(diag['returned_complete_fallback'])

    def test_fixed_outside_order_survives(self):
        ir = graph([20, 30, 40, 50])
        base = plan([[0], [1], [2], [3]], [0, 0, 1, 1])
        model = _raw_model(ir, base, [[0], [1], [2], [3]], {0: 0, 1: 0}, [0, 0, 1, 1], extra_preds=[set(), {0}, set(), set()])
        result, diag = solve_beam(ir, model, width=8)
        self.assertLess(result['core_schedules'][0].index(model['mapping'][0]), result['core_schedules'][0].index(model['mapping'][1]))
        validate_plan(ir, result)

    def test_coarse_rebuild_preserves_fixed_order(self):
        ir = graph([20, 30, 40, 50])
        base = plan([[0], [1], [2], [3]], [0, 0, 1, 1])
        model = _raw_model(ir, base, [[0], [1], [2], [3]], {0: 0, 1: 0}, [0, 0, 1, 1], extra_preds=[set(), {0}, set(), set()])
        coarse = model_with_blocks(ir, model, [[1], [2, 3], [0]])
        self.assertEqual(len(coarse['active']), 1)
        a, b = coarse['mapping'][0], coarse['mapping'][1]
        self.assertIn(a, coarse['preds'][b])
        result, _ = solve_beam(ir, coarse)
        validate_plan(ir, result)
        with self.assertRaises(ValueError): model_with_blocks(ir, model, [[0, 2], [1], [3]])

    def test_small_trace_build_releases_halo_ownership(self):
        ir = graph([10, 10, 10], [(0, 1, 0), (1, 2, 0)])
        base = plan([[0], [1], [2]], [0, 0, 0])
        raw = dict(makespan=230, task_cross_core_wait_cycles=1000, task_same_core_wait_cycles=100,
                   per_core_timeline=[dict(tasks=[dict(task_id=0,start=0,end=10), dict(task_id=1,start=110,end=120), dict(task_id=2,start=220,end=230)],
                   ops=[dict(op_id=0,start=0,end=10),dict(op_id=1,start=110,end=120),dict(op_id=2,start=220,end=230)]),dict(tasks=[],ops=[])])
        model = build_problem(ir, base, raw)
        self.assertTrue(model['metadata']['released_halo_tasks'])
        for t in model['metadata']['released_halo_tasks']:
            self.assertNotIn(model['mapping'][t], model['fixed'])


if __name__ == '__main__': unittest.main()
