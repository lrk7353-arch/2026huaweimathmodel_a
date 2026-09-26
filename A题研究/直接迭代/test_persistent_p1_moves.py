"""Counterexamples for partial-Task repairs and incremental budget accounting."""
import copy
import random
import time
import unittest
from unittest.mock import patch

from common_run import validate_plan
from test_p1_task_refine import graph, owners
from p1_selective import topological_order
from p1_bottleneck_repartition import schedule
from persistent_p1_moves import (iter_candidates, prepare_region, select_region,
                                 _trace, _acyclic_pieces, _merge_small)


class PersistentP1Tests(unittest.TestCase):
    def test_partial_task_residual_cycle_is_split_not_merged(self):
        ir = graph(3, [(0, 1), (1, 2)])
        old = dict(node_to_subgraph={str(i): 0 for i in range(3)}, core_schedules=[[0], []])
        blocks, bv, fixed, preds, metadata = prepare_region(
            ir, old, {1}, [[1]], [0, 1, 2], time.perf_counter()+2)
        self.assertEqual(len(blocks), 3)
        self.assertEqual(metadata['boundary_repair_ops'], 2)
        candidate, _ = schedule(ir, old, blocks, bv, fixed, preds)
        validate_plan(ir, candidate)
        self.assertEqual(owners(candidate)['0'], 0)
        self.assertEqual(owners(candidate)['2'], 0)

    def test_cross_old_task_region_removes_artificial_barrier(self):
        ir = graph(6, [(0, 3), (3, 4), (1, 2), (2, 5)])
        old = dict(node_to_subgraph={str(i): i//2 for i in range(6)}, core_schedules=[[0, 1, 2], []])
        before = copy.deepcopy((ir.graph, old))
        candidates = list(iter_candidates(ir, old, {}, 2, round_index=1))
        self.assertTrue(candidates)
        self.assertTrue(any(c['metadata']['cross_old_boundary_blocks'] for c in candidates))
        for c in candidates:
            validate_plan(ir, c['plan'])
            self.assertTrue(c['metadata']['changed_old_boundaries'])
        self.assertEqual((ir.graph, old), before)

    def test_giant_task_window_is_not_skipped(self):
        ir = graph(9000, [])
        old = dict(node_to_subgraph={str(i): 0 for i in range(9000)}, core_schedules=[[0], []])
        region, metadata = select_region(ir, _trace(ir, old, {}, None), 64, 0, time.perf_counter()+3)
        self.assertEqual(len(region), 64)
        self.assertEqual(metadata['partial_old_tasks'], [0])

    def test_unselected_whole_task_encoding_and_owner_are_protected(self):
        ir = graph(8, [(0, 2), (1, 3), (2, 4), (3, 5)])
        old = dict(node_to_subgraph={str(i): i for i in range(8)}, core_schedules=[[0, 2, 4, 6], [1, 3, 5, 7]])
        blocks, bv, fixed, preds, metadata = prepare_region(
            ir, old, {2, 3}, [[2], [3]], topological_order(ir, 'stable_id'), time.perf_counter()+2)
        candidate, _ = schedule(ir, old, blocks, bv, fixed, preds)
        owner = owners(candidate)
        for core, ops in enumerate(((0, 4, 6), (1, 5, 7))):
            tids = [candidate['node_to_subgraph'][str(op)] for op in ops]
            self.assertEqual([candidate['core_schedules'][core].index(t) for t in tids],
                             sorted(candidate['core_schedules'][core].index(t) for t in tids))
            self.assertTrue(all(owner[str(op)] == core for op in ops))

    def test_random_partition_cycles_are_repaired_without_coverage_loss(self):
        rng = random.Random(6491)
        for _ in range(30):
            n = 30
            ir = graph(n, [(a, b) for a in range(n) for b in range(a+1, n) if rng.random() < .08])
            groups = [[] for _ in range(5)]
            for op in range(n): groups[rng.randrange(5)].append(op)
            blocks, _ = _acyclic_pieces(ir, [g for g in groups if g], list(range(n)), time.perf_counter()+2)
            mp = {op: b for b, ns in enumerate(blocks) for op in ns}
            self.assertEqual(set(mp), set(range(n)))
            self.assertTrue(all(mp[a] <= mp[b] for a in range(n) for b in ir.successors[a]))

    def test_cycle_repair_does_not_split_acyclic_downstream_group(self):
        ir = graph(5, [(0, 1), (1, 2), (1, 3), (2, 4)])
        blocks, cut = _acyclic_pieces(ir, [[0, 2], [1], [3, 4]], list(range(5)), time.perf_counter()+2)
        self.assertIn([3, 4], blocks)
        self.assertNotIn(3, cut)

    def test_regional_merge_preserves_remote_dependency_path(self):
        ir = graph(4, [(0, 1), (1, 2)])
        old = dict(node_to_subgraph={str(i): i for i in range(4)}, core_schedules=[[0, 2, 3], [1]])
        candidate, merges = _merge_small(ir, old, {0, 1, 2, 3}, 99999, time.perf_counter()+2)
        validate_plan(ir, candidate)
        self.assertNotEqual(candidate['node_to_subgraph']['0'], candidate['node_to_subgraph']['2'])
        self.assertGreater(merges, 0)

    def test_yield_suspension_does_not_spend_generation_budget(self):
        ir = graph(100, [(i, i+1) for i in range(0, 99, 2)])
        old = dict(node_to_subgraph={str(i): 0 for i in range(100)}, core_schedules=[[0], [], [], [], []])
        offset = [0.]
        real_clock = time.perf_counter
        with patch('persistent_p1_moves.time.perf_counter', side_effect=lambda: real_clock()+offset[0]):
            gen = iter_candidates(ir, old, {}, 5, seconds=5)
            first = next(gen)
            offset[0] += 1000.
            remaining = list(gen)
        self.assertTrue(remaining)
        self.assertLess(remaining[-1]['metadata']['generation_seconds_cumulative'], 5)

    def test_input_name_and_zero_budget(self):
        ir = graph(10, [])
        old = dict(node_to_subgraph={str(i): 0 for i in range(10)}, core_schedules=[[0], []])
        a = [c['plan'] for c in iter_candidates(ir, old, {}, 2)]
        ir.path = 'unseen_graph.json'
        self.assertEqual(a, [c['plan'] for c in iter_candidates(ir, old, {}, 2)])
        self.assertEqual([], list(iter_candidates(ir, old, {}, 2, seconds=0)))

    def test_outer_timeout_and_unyielded_rejections_remain_visible(self):
        ir = graph(10, [])
        old = dict(node_to_subgraph={str(i): 0 for i in range(10)}, core_schedules=[[0], []])
        with patch('persistent_p1_moves._check', side_effect=TimeoutError('outer alarm')):
            with self.assertRaisesRegex(TimeoutError, 'outer alarm'):
                next(iter_candidates(ir, old, {}, 2))
        with patch('persistent_p1_moves.schedule', side_effect=ValueError('outside-order conflict')):
            gen = iter_candidates(ir, old, {}, 2)
            with self.assertRaises(StopIteration) as stopped:
                next(gen)
        self.assertEqual(len(stopped.exception.value['generation_failures']), 2)


if __name__ == '__main__':
    unittest.main()
