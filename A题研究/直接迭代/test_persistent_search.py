"""Regression checks for the actual search-lifecycle failure mechanisms."""
import unittest
from types import SimpleNamespace
import tempfile
from pathlib import Path
from unittest.mock import patch
import time

from persistent_budget import PersistentBudget, structural_signature, exact_signature


def record(name, span, copied=0):
    return dict(status='success', record_path=name,
                metrics=dict(makespan=span, data_movement_bytes=dict(added_copy_bytes=copied)))


class LifecycleTests(unittest.TestCase):
    def test_old_joint_queue_reaches_fifo_before_unrelated_next_family(self):
        from persistent_search import Frame
        parent = Frame(record('old_parent', 100), 0)
        parent.streams['joint'] = iter(['placement', 'fifo_second_action'])
        self.assertEqual(next(parent.streams['joint']), 'placement')
        parent.cursor = 2  # the ordinary P3 round robin would now choose cache
        parent.served('joint')
        parent.protect_queue('joint')
        order = ['insertion', 'joint', 'cache', 'trace']
        self.assertEqual(parent.next_family(order), 'joint')
        self.assertEqual(next(parent.streams['joint']), 'fifo_second_action')
        parent.served('joint')
        self.assertEqual(parent.next_family(order), 'cache')

    def test_stale_parent_repair_uses_its_own_iterator_not_currents(self):
        from persistent_search import Frame
        old = Frame(record('actual_parent', 300), 0)
        current = Frame(record('current_best', 100), 2)
        old.streams['joint'] = iter(['old_first', 'old_fifo'])
        current.streams['joint'] = iter(['current_first', 'current_fifo'])
        self.assertEqual(next(old.streams['joint']), 'old_first')
        old.protect_queue('joint')
        self.assertEqual(old.next_family(['cache', 'joint']), 'joint')
        self.assertEqual(next(old.streams['joint']), 'old_fifo')
        self.assertEqual(next(current.streams['joint']), 'current_first')
        self.assertEqual(current.stale_lease, 0)

    def test_same_structure_keeps_old_paid_order_and_its_lease(self):
        control = PersistentBudget(1, 2)
        control.add_seed(record('old_order', 300), 'same', 'old')
        control.add_seed(record('new_order', 100), 'same', 'new')
        control.add_seed(record('waiting', 400), 'other', 'other')
        control.start()
        branch = control.choose()
        self.assertEqual(branch.alternate_seeds[0]['record']['record_path'], 'old_order')
        branch.frames = [SimpleNamespace(stale_lease=1)]
        control.observe(branch)
        control.observe(branch)
        self.assertIs(control.choose(), branch)
        branch.frames[0].stale_lease = 0
        self.assertEqual(control.choose().origin, 'other')

    def test_nested_generation_cannot_extend_outer_limit(self):
        from persistent_search import generation_limit
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            with generation_limit(.02):
                with generation_limit(1):
                    time.sleep(.06)
        self.assertLess(time.monotonic()-start, .5)

    def test_failed_initial_evaluation_spends_the_only_call(self):
        from persistent_search import run
        plan = dict(node_to_subgraph={'1': 0}, core_schedules=[[0]])
        proposals = [('one', lambda: dict(name='one', plan=plan)),
                     ('two', lambda: dict(name='two', plan=plan))]
        with tempfile.TemporaryDirectory() as tmp:
            with patch('persistent_search.GraphIR.from_path', return_value=SimpleNamespace(path=Path(tmp)/'graph.json')), \
                 patch('persistent_search.factories', return_value=proposals), \
                 patch('persistent_search.validate_plan'), \
                 patch('persistent_search.evaluate', return_value=dict(status='timeout', cache_hit=False)) as evaluator:
                result = run('case_001', 2, 1, Path(tmp)/'run', budget=1, seconds=10, timeout=1)
        self.assertEqual(evaluator.call_count, 1)
        self.assertEqual(result['logical_calls'], 1)
        self.assertEqual(result['failed_calls'], 1)
        self.assertIsNone(result['best_record'])
        self.assertTrue(result['complete'])

    def test_slow_structure_gets_real_budget_before_completed_branch(self):
        control = PersistentBudget(3, 2)
        for name, cost in [('slow', 300), ('best', 100), ('other', 150)]:
            control.add_seed(record(name, cost), name, name)
        control.start()
        served = []
        for _ in range(6):
            branch = control.choose()
            served.append(branch.origin)
            control.observe(branch)
        self.assertEqual({x: served.count(x) for x in served}, dict(slow=2, best=2, other=2))

    def test_improvement_inherits_visits_and_lease(self):
        control = PersistentBudget(2, 2)
        control.add_seed(record('best', 100), 'a', 'a')
        control.add_seed(record('slow', 300), 'b', 'b')
        control.start()
        branch = control.choose()
        control.observe(branch, record('slightly_better', 99), 'new_a')
        self.assertEqual(branch.visits, 1)
        self.assertEqual(branch.protected_remaining, 1)
        self.assertEqual(control.choose().origin, 'b')
        self.assertEqual(branch.depth, 1)

    def test_local_improvement_survives_without_global_record(self):
        control = PersistentBudget(2, 2)
        control.add_seed(record('global', 100), 'a', 'a')
        control.add_seed(record('local', 500), 'b', 'b')
        control.start()
        branch = next(b for b in control.active if b.origin == 'b')
        self.assertTrue(control.observe(branch, record('local_child', 250), 'c'))
        self.assertEqual(branch.record['record_path'], 'local_child')
        self.assertEqual(branch.depth, 1)

    def test_completed_stalled_lineage_rotates_to_untried_seed(self):
        control = PersistentBudget(1, 2)
        control.add_seed(record('best', 100), 'a', 'a')
        control.add_seed(record('slow', 300), 'b', 'b')
        control.start()
        branch = control.choose()
        control.observe(branch)
        self.assertIs(control.choose(), branch)
        control.observe(branch)
        self.assertEqual(control.choose().origin, 'b')
        self.assertEqual(control.retired[0].visits, 2)

    def test_failures_also_consume_protected_quota(self):
        control = PersistentBudget(1, 2)
        control.add_seed(record('parent', 100), 'a', 'a')
        control.start()
        branch = control.choose()
        control.observe(branch, dict(status='timeout'))
        self.assertEqual(branch.protected_remaining, 1)
        self.assertEqual(branch.record['record_path'], 'parent')

    def test_core_labels_and_order_do_not_create_fake_diversity(self):
        p = dict(node_to_subgraph={'1': 0, '2': 1, '3': 2, '4': 3}, core_schedules=[[0, 2], [1, 3]])
        q = dict(node_to_subgraph={'4': 30, '3': 20, '2': 10, '1': 0}, core_schedules=[[30, 10], [20, 0]])
        self.assertNotEqual(exact_signature(p), exact_signature(q))
        self.assertEqual(structural_signature(p, 2), structural_signature(q, 2))
        self.assertEqual(structural_signature(p, 1), structural_signature(q, 1))
        # Changing the P1 partition matters even with unchanged ownership.
        r = dict(node_to_subgraph={'1': 0, '2': 1, '3': 0, '4': 1}, core_schedules=[[0], [1]])
        self.assertNotEqual(structural_signature(p, 1), structural_signature(r, 1))
        self.assertEqual(structural_signature(p, 3), structural_signature(r, 3))


if __name__ == '__main__':
    unittest.main()
