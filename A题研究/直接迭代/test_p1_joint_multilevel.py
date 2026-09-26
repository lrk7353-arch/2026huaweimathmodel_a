"""Structural counterexamples for a real, inherited two-level P1 hierarchy."""
import copy
import time
import unittest
from unittest.mock import patch

from common_run import validate_plan
from test_p1_task_refine import graph, owners
from p1_joint_multilevel import (_quotient, coarsen_groups, project_solution,
                                 refinement_model, solve_multilevel)


def model_for(ir, active=None, fixed=None, protected=None):
    count = len(ir.compute_ids)
    fixed = dict(fixed or {})
    active = set(range(count)) - set(fixed) if active is None else set(active)
    incumbent = {'node_to_subgraph': {str(op): op for op in ir.compute_ids},
                 'core_schedules': [list(ir.compute_ids), []]}
    for bid, core in fixed.items():
        if core != 0:
            incumbent['core_schedules'][0].remove(bid)
            incumbent['core_schedules'][core].append(bid)
    return dict(blocks=[[op] for op in ir.compute_ids],
                preds=[set(ir.predecessors[op]) for op in ir.compute_ids],
                durations=[ir.ops[op]['cycles'] for op in ir.compute_ids],
                active=sorted(active), fixed=fixed, incumbent=incumbent,
                ncores=2, metadata={'protected_pairs': protected or []})


class MultilevelTests(unittest.TestCase):
    def test_remote_outside_path_prevents_active_contraction(self):
        ir = graph(5, [(0, 1), (1, 2), (0, 2), (3, 4)])
        model = model_for(ir, fixed={1: 1})
        before = copy.deepcopy(model)
        groups, diagnostics = coarsen_groups(model, time.perf_counter()+2)
        mapping, _, _ = _quotient(model['preds'], groups)
        self.assertNotEqual(mapping[0], mapping[2])
        self.assertEqual(mapping[3], mapping[4])
        self.assertGreater(diagnostics['rejected_cyclic_contractions'], 0)
        self.assertEqual(model, before)

    def test_outside_core_order_alone_also_prevents_contraction(self):
        ir = graph(5, [(0, 2), (3, 4)])
        model = model_for(ir, fixed={1: 1})
        model['preds'][1].add(0)
        model['preds'][2].add(1)
        groups, diagnostics = coarsen_groups(model)
        mapping, _, _ = _quotient(model['preds'], groups)
        self.assertNotEqual(mapping[0], mapping[2])
        self.assertGreater(diagnostics['rejected_cyclic_contractions'], 0)

    def test_critical_producer_cannot_swallow_a_later_tail(self):
        ir = graph(6, [(op, op+1) for op in range(5)])
        model = model_for(ir, protected=[[1, 2]])
        groups, diagnostics = coarsen_groups(model)
        self.assertIn([1], groups)
        self.assertIn([2], groups)
        self.assertTrue(diagnostics['contractions'])
        self.assertEqual(diagnostics['protected_singletons'], [1, 2])

    def test_projection_inherits_actual_coarse_core_and_order(self):
        ir = graph(5, [(0, 1), (1, 2), (2, 3)])
        fine = model_for(ir, fixed={4: 0})
        # Deliberately preserve a non-sorted input-key order.
        fine['incumbent']['node_to_subgraph'] = {str(op): op for op in [4, 0, 3, 1, 2]}
        coarse = {'blocks': [[0, 1], [2, 3], [4]]}
        plan = {'node_to_subgraph': {'0': 73, '1': 73, '2': 52, '3': 52, '4': 99},
                'core_schedules': [[99], [73, 52]]}
        projected, mapping = project_solution(ir, fine, coarse, plan)
        validate_plan(ir, projected)
        self.assertEqual(projected['core_schedules'], [[4], [0, 1, 2, 3]])
        self.assertEqual(list(projected['node_to_subgraph']), ['4', '0', '3', '1', '2'])
        self.assertEqual(mapping, {0: 0, 1: 0, 2: 1, 3: 1, 4: 2})
        self.assertEqual(owners(projected), {'4': 0, '0': 1, '3': 1, '1': 1, '2': 1})

    def test_fine_repair_retains_coarse_interiors_and_order(self):
        ir = graph(9, [(op, op+1) for op in range(8)])
        fine = model_for(ir, fixed={8: 0}, protected=[[3, 4]])
        coarse = {'blocks': [[0, 1], [2, 3], [4, 5], [6, 7], [8]]}
        plan = {'node_to_subgraph': {str(op): op//2 for op in range(9)},
                'core_schedules': [[0, 2, 4], [1, 3]]}
        projection, mapping = project_solution(ir, fine, coarse, plan)
        refined, preferred, diagnostics = refinement_model(fine, projection, mapping)
        self.assertTrue(diagnostics['projected_fixed_active_blocks'])
        self.assertIn(3, refined['active'])
        self.assertIn(4, refined['active'])
        original_owner = owners(projection)
        for bid in diagnostics['projected_fixed_active_blocks']:
            self.assertEqual(refined['fixed'][bid], original_owner[str(bid)])
        for first, second in diagnostics['inherited_core_order_edges']:
            self.assertIn(first, refined['preds'][second])
        self.assertEqual(preferred['owner'][2], 1)
        self.assertEqual(preferred['order'], [0, 1, 4, 5, 8, 2, 3, 6, 7])
        self.assertEqual(fine['fixed'], {8: 0})

    def test_duplicate_fine_coverage_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'multiple'):
            _quotient([set(), {0}], [[0], [0, 1]])

    def test_real_frontier_interface_preserves_two_level_lineage(self):
        import p1_joint_frontier as frontier
        ir = graph(12, [(op, op+1) for op in range(11)])
        incumbent = {'node_to_subgraph': {str(op): op for op in range(12)},
                     'core_schedules': [list(range(12)), []]}
        model = frontier._raw_model(ir, incumbent, [[op] for op in range(12)],
                                    {11: 0}, [0]*12, {'protected_pairs': [(1, 2)]})
        candidate, diagnostics = solve_multilevel(ir, model, time.perf_counter()+3, width=2)
        validate_plan(ir, candidate)
        self.assertEqual(diagnostics['fine_total_blocks'], 12)
        self.assertLess(diagnostics['coarse_total_blocks'], 12)
        self.assertEqual(len(diagnostics['fine_to_coarse']), 12)
        self.assertTrue(diagnostics['projected_fixed_active_blocks'])
        self.assertEqual(candidate['node_to_subgraph'], incumbent['node_to_subgraph'])
        self.assertIn(diagnostics['selected_stage'], ['coarse_projection', 'boundary_refinement'])

    def test_refinement_timeout_returns_legal_projection_and_one_shared_deadline(self):
        import p1_joint_frontier as frontier
        ir = graph(8, [(op, op+1) for op in range(7)])
        incumbent = {'node_to_subgraph': {str(op): op for op in range(8)},
                     'core_schedules': [list(range(8)), []]}
        model = frontier._raw_model(ir, incumbent, [[op] for op in range(8)], {}, [0]*8)
        original_solve = frontier.solve_beam
        received = []
        def fake_solver(*args, **kwargs):
            received.append(kwargs['deadline'])
            if len(received) == 2:
                raise TimeoutError('synthetic fine repair timeout')
            return original_solve(*args, **kwargs)
        deadline = time.perf_counter()+3
        with patch.object(frontier, 'solve_beam', side_effect=fake_solver):
            candidate, diagnostics = solve_multilevel(ir, model, deadline, width=2)
        validate_plan(ir, candidate)
        self.assertEqual(diagnostics['selected_stage'], 'coarse_projection')
        self.assertEqual(diagnostics['fine_solver']['status'], 'deadline')
        self.assertLess(received[0], deadline)
        self.assertEqual(received[1], deadline)
        self.assertIsNone(diagnostics['refinement_score'])


if __name__ == '__main__':
    unittest.main()
