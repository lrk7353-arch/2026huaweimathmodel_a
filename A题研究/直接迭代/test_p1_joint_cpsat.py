"""Small scheduling counterexamples; never invokes an official evaluator."""
import copy
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from common_run import validate_plan
from p1_joint_cpsat import SCALE, solve_cpsat
from test_p1_task_refine import graph


def problem(durations, edges=(), ncores=2, fixed=None, preferred=None,
            outside_edges=()):
    size = len(durations)
    ir = graph(size, edges)
    preds = [set() for _ in durations]
    for parent, child in tuple(edges)+tuple(outside_edges):
        preds[child].add(parent)
    fixed = {} if fixed is None else dict(fixed)
    preferred = [fixed.get(b, b % ncores) for b in range(size)] if preferred is None else preferred
    schedules = [[] for _ in range(ncores)]
    # Tests use topologically numbered operations for the incumbent.
    for b in range(size):
        schedules[fixed.get(b, preferred[b])].append(b)
    plan = {'node_to_subgraph': {str(b): b for b in range(size)},
            'core_schedules': schedules}
    model = dict(blocks=[[b] for b in range(size)], preds=preds,
                 fixed=fixed, active=[b for b in range(size) if b not in fixed],
                 durations=durations, ncores=ncores, incumbent=plan,
                 preferred_owner=preferred, metadata={})
    return ir, model


class UnknownSolver:
    """Deterministically exercise a solver timeout without spending seconds."""
    seen = None

    def __init__(self):
        self.parameters = SimpleNamespace()
        self.wall_time = 0.0
        self.num_conflicts = self.num_branches = 0

    def solve(self, cp):
        from ortools.sat.python import cp_model
        type(self).seen = cp.proto
        return cp_model.UNKNOWN

    def status_name(self, status):
        return 'UNKNOWN'


class CPSATJointTests(unittest.TestCase):
    def solve(self, ir, model):
        plan, diag = solve_cpsat(ir, model, deadline=time.perf_counter()+5)
        self.assertIsNotNone(plan, diag)
        self.assertEqual(diag['status'], 'OPTIMAL', diag)
        validate_plan(ir, plan)
        return plan, diag

    def test_first_task_has_no_same_core_start_penalty(self):
        ir, model = problem([10], ncores=1)
        _, diag = self.solve(ir, model)
        self.assertEqual(diag['proxy_makespan'], 10)

    def test_dependent_active_tasks_choose_same_core_to_avoid_cross_wait(self):
        ir, model = problem([10, 10], edges=[(0, 1)])
        plan, diag = self.solve(ir, model)
        self.assertEqual(diag['proxy_makespan'], 120)
        self.assertEqual(diag['owners'][0], diag['owners'][1])
        self.assertEqual(sum(bool(seq) for seq in plan['core_schedules']), 1)

    def test_forced_cross_dependency_uses_1000_not_1100(self):
        ir, model = problem([10, 1, 10], edges=[(0, 2)],
                            fixed={0: 0, 1: 1, 2: 1}, outside_edges=[(1, 2)])
        _, diag = self.solve(ir, model)
        self.assertEqual(diag['proxy_makespan'], 1020)
        self.assertEqual(diag['starts_cycles'][2], 1010)

    def test_core_and_order_are_free_for_active_block(self):
        ir, model = problem([3000, 100, 2000], fixed={0: 0, 1: 1},
                            preferred=[0, 1, 0])
        _, diag = self.solve(ir, model)
        self.assertEqual(diag['owners'][2], 1)
        self.assertEqual(diag['proxy_makespan'], 3000)

    def test_fixed_outside_core_and_relative_order_are_retained(self):
        ir, model = problem([200, 400, 100, 800], edges=[(0, 3)],
                            fixed={0: 1, 2: 1}, outside_edges=[(0, 2)])
        plan, diag = self.solve(ir, model)
        self.assertEqual(diag['owners'][0], 1)
        self.assertEqual(diag['owners'][2], 1)
        self.assertLess(plan['core_schedules'][1].index(0),
                        plan['core_schedules'][1].index(2))

    def test_external_dependency_path_prevents_cycle(self):
        ir, model = problem([10, 10, 10], edges=[(0, 1), (1, 2)],
                            fixed={0: 0, 2: 0}, outside_edges=[(0, 2)])
        plan, diag = self.solve(ir, model)
        self.assertEqual(diag['proxy_makespan'], 230)
        self.assertEqual(plan['core_schedules'][0], [0, 1, 2])

    def test_cycle_in_union_with_outside_order_is_rejected(self):
        ir, model = problem([10, 10, 10], edges=[(0, 1), (1, 2)],
                            fixed={0: 0, 2: 0}, outside_edges=[(2, 0)])
        with self.assertRaisesRegex(ValueError, 'cycle'):
            solve_cpsat(ir, model)

    def test_fractional_durations_represented_exactly(self):
        ir, model = problem([10+1/60, 10+7/60], edges=[(0, 1)], ncores=1)
        _, diag = self.solve(ir, model)
        self.assertEqual(diag['objective_ticks'], 120*SCALE+8)
        self.assertLess(diag['max_duration_rounding_cycles'], 1e-9)

    def test_nonrepresentable_duration_rounds_up_and_reports_error(self):
        ir, model = problem([10.001], ncores=1)
        _, diag = self.solve(ir, model)
        self.assertEqual(diag['objective_ticks'], 601)
        self.assertGreater(diag['max_duration_rounding_cycles'], 0)
        self.assertLess(diag['max_duration_rounding_cycles'], 1/SCALE)

    def test_input_and_mapping_key_order_are_unchanged(self):
        ir, model = problem([10, 20, 30], edges=[(0, 2)])
        model['incumbent']['node_to_subgraph'] = {'2': 2, '0': 0, '1': 1}
        saved = copy.deepcopy(model)
        plan, _ = self.solve(ir, model)
        self.assertEqual(saved, model)
        self.assertEqual(list(plan['node_to_subgraph']), ['2', '0', '1'])

    def test_exact_common_model_matches_complete_plan_proxy(self):
        from p1_joint_frontier import _raw_model, score_plan
        ir, sample = problem([10, 10, 10, 10],
                             edges=[(0, 2), (1, 2), (2, 3)])
        model = _raw_model(ir, sample['incumbent'], sample['blocks'], {},
                           sample['preferred_owner'])
        plan, diag = self.solve(ir, model)
        self.assertAlmostEqual(score_plan(ir, plan)['proxy'],
                               diag['proxy_makespan'], places=7)

    def test_expired_deadline_returns_no_candidate(self):
        ir, model = problem([10])
        plan, diag = solve_cpsat(ir, model, deadline=time.perf_counter()-1)
        self.assertIsNone(plan)
        self.assertEqual(diag['status'], 'CONSTRUCTION_TIMEOUT')

    def test_invalid_core_or_partition_description_is_rejected(self):
        ir, model = problem([10])
        model['fixed'] = {0: 99}
        model['active'] = []
        with self.assertRaisesRegex(ValueError, 'core range'):
            solve_cpsat(ir, model)
        ir, model = problem([10])
        model['active'] = []
        with self.assertRaisesRegex(ValueError, 'partition'):
            solve_cpsat(ir, model)

    def test_strong_warm_plan_supplies_upper_bound_and_complete_hint(self):
        ir, model = problem([10, 20, 30], edges=[(0, 2)], fixed={0: 0, 1: 0})
        warm = {'node_to_subgraph': {'0': 0, '1': 1, '2': 2},
                'core_schedules': [[0, 1, 2], []]}
        with patch('ortools.sat.python.cp_model.CpSolver', UnknownSolver):
            plan, diag = solve_cpsat(ir, model, warm_plan=warm)
        self.assertEqual(plan, warm)
        self.assertIsNot(plan, warm)
        self.assertEqual(diag['warm_objective_ticks'], 260*SCALE)
        self.assertEqual(diag['status'], 'UNKNOWN')
        self.assertEqual(diag['returned'], 'verified_warm_fallback')
        proto = UnknownSolver.seen
        makespan = next(i for i, v in enumerate(proto.variables) if v.name == 'makespan')
        self.assertTrue(any(list(c.linear.vars) == [makespan]
                            and list(c.linear.coeffs) == [1]
                            and list(c.linear.domain)[-1] == 260*SCALE for c in proto.constraints
                            if len(c.linear.vars)))
        self.assertEqual(set(proto.solution_hint.vars), set(range(len(proto.variables))))
        self.assertEqual(len(proto.solution_hint.vars), len(set(proto.solution_hint.vars)))

    def test_warm_candidate_can_be_strictly_improved(self):
        ir, model = problem([3000, 100, 2000], fixed={0: 0, 1: 1}, preferred=[0, 1, 0])
        warm = copy.deepcopy(model['incumbent'])
        plan, diag = solve_cpsat(ir, model, warm_plan=warm)
        validate_plan(ir, plan)
        self.assertEqual(diag['warm_proxy_makespan'], 5100)
        self.assertEqual(diag['proxy_makespan'], 3000)
        self.assertEqual(diag['returned'], 'cp_proxy_improvement')
        self.assertFalse(diag['warm_fallback'])
        self.assertEqual(diag['proxy_gain_cycles'], 2100)
        self.assertEqual(warm, model['incumbent'])

    def test_equal_proxy_returns_original_warm_encoding(self):
        ir, model = problem([10, 10], edges=[(0, 1)])
        warm = {'node_to_subgraph': {'1': 1, '0': 0}, 'core_schedules': [[0, 1], []]}
        plan, diag = solve_cpsat(ir, model, warm_plan=warm)
        self.assertEqual(plan, warm)
        self.assertEqual(list(plan['node_to_subgraph']), ['1', '0'])
        self.assertEqual(diag['fallback_reason'], 'no_strict_proxy_gain')
        self.assertEqual(diag['proxy_makespan'], 120)

    def test_warm_partition_mismatch_is_rejected(self):
        ir, model = problem([10, 10])
        warm = {'node_to_subgraph': {'0': 0, '1': 0}, 'core_schedules': [[0], []]}
        with self.assertRaisesRegex(ValueError, 'exact common model partition'):
            solve_cpsat(ir, model, warm_plan=warm)

    def test_warm_core_domain_and_fixed_owner_are_rejected(self):
        ir, model = problem([10], fixed={0: 1})
        warm = {'node_to_subgraph': {'0': 0}, 'core_schedules': [[], [], [0]]}
        with self.assertRaisesRegex(ValueError, 'owner domain'):
            solve_cpsat(ir, model, warm_plan=warm)
        warm['core_schedules'] = [[0], []]
        with self.assertRaisesRegex(ValueError, 'fixed outside core'):
            solve_cpsat(ir, model, warm_plan=warm)

    def test_warm_order_conflict_with_extra_model_pred_is_rejected(self):
        ir, model = problem([10, 10], outside_edges=[(0, 1)], fixed={0: 0, 1: 0})
        warm = {'node_to_subgraph': {'0': 0, '1': 1}, 'core_schedules': [[1, 0], []]}
        validate_plan(ir, warm)  # Original graph alone cannot detect this error.
        with self.assertRaisesRegex(ValueError, 'conflicts with model'):
            solve_cpsat(ir, model, warm_plan=warm)

    def test_warm_operation_coverage_is_checked(self):
        ir, model = problem([10, 10])
        warm = {'node_to_subgraph': {'0': 0}, 'core_schedules': [[0], []]}
        with self.assertRaisesRegex(ValueError, 'exactly cover'):
            solve_cpsat(ir, model, warm_plan=warm)

    def test_timeout_after_warm_validation_retains_verified_warm(self):
        ir, model = problem([10])
        warm = copy.deepcopy(model['incumbent'])
        # The first check admits warm validation; the second detects expiration.
        with patch('p1_joint_cpsat._check', side_effect=[None, TimeoutError('test timeout')]):
            plan, diag = solve_cpsat(ir, model, warm_plan=warm)
        self.assertEqual(plan, warm)
        self.assertTrue(diag['warm_validated'])
        self.assertEqual(diag['status'], 'CONSTRUCTION_TIMEOUT')
        self.assertEqual(diag['fallback_reason'], 'construction_deadline_after_warm_validation')

    def test_unknown_without_warm_keeps_original_none_behavior(self):
        ir, model = problem([10])
        with patch('ortools.sat.python.cp_model.CpSolver', UnknownSolver):
            plan, diag = solve_cpsat(ir, model)
        self.assertIsNone(plan)
        self.assertEqual(diag['status'], 'UNKNOWN')
        self.assertFalse(diag['hybrid'])


if __name__ == '__main__':
    unittest.main()
