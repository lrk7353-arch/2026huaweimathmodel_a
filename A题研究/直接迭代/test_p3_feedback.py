"""Controller contracts with synthetic plans; no official evaluator is invoked."""
import copy
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import p3_feedback as controller


def plan(label):
    return {
        'node_to_subgraph': {'10': label, '20': label + 1},
        'core_schedules': [[label, label + 1], []],
    }


def candidate(name, value, control=False):
    return {
        'name': name,
        'plan': copy.deepcopy(value),
        'metadata': {
            'is_reencoding_control': control,
            'mechanism': 'control' if control else 'synthetic_move',
        },
    }


class FeedbackControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.initial = plan(0)
        self.old = self.record('initial', self.initial, makespan=100, cache_hit=False)
        self.evaluated = []
        self.outcomes = {}
        self.read_sources = []
        self.legacy_rounds = []
        self.legacy_batches = {}
        self.read_batches = []

    def record(self, name, value, makespan=100, status='success', cache_hit=False,
               cost=2.0, copy_bytes=10):
        location = self.root / name
        location.mkdir()
        plan_path = location / 'plan.json'
        plan_path.write_text(json.dumps(value), encoding='utf-8')
        raw_path = location / 'result.json.gz'
        with gzip.open(raw_path, 'wt', encoding='utf-8') as stream:
            json.dump({'source': name, 'makespan': makespan}, stream)
        result = {
            'status': status, 'cache_hit': cache_hit,
            'graph_path': str(self.root / 'case_001.json'), 'problem': 3,
            'plan_path': str(plan_path), 'result_path': str(raw_path),
            'record_path': str(location / 'record.json'),
            'evaluation_elapsed_seconds': cost,
            'metrics': ({'num_cores': 2, 'makespan': makespan,
                         'data_movement_bytes': {'added_copy_bytes': copy_bytes}}
                        if status == 'success' else {}),
        }
        (location / 'record.json').write_text(json.dumps(result), encoding='utf-8')
        return result

    def mock_evaluate(self, graph, value, problem, directory, **kwargs):
        self.evaluated.append(copy.deepcopy(value))
        self.assertEqual(problem, 3)
        self.assertGreater(kwargs['timeout'], 0)
        self.assertLessEqual(kwargs['timeout'], 60)
        outcome = self.outcomes.get(controller.exact(value), {})
        return self.record('evaluation_' + str(len(self.evaluated)), value, **outcome)

    def mock_legacy(self, ir, value, raw, **kwargs):
        self.legacy_rounds.append(kwargs['round_index'])
        return copy.deepcopy(self.legacy_batches.get(kwargs['round_index'], [])), {}

    def mock_read(self, ir, value, raw, cores, limit):
        self.assertEqual(cores, 2)
        self.read_sources.append((copy.deepcopy(value), copy.deepcopy(raw)))
        index = len(self.read_sources) - 1
        batch = self.read_batches[index] if index < len(self.read_batches) else []
        return copy.deepcopy(batch), {}

    def run_controller(self, budget, policy='feedback', **kwargs):
        with patch.object(controller.GraphIR, 'from_path', return_value=object()), \
             patch.object(controller, 'validate_plan', return_value=True), \
             patch.object(controller, 'generate_cache_candidates', side_effect=self.mock_legacy), \
             patch.object(controller, 'read_candidates', side_effect=self.mock_read), \
             patch.object(controller, 'evaluate', side_effect=self.mock_evaluate) as evaluator:
            result = controller.run('case_001', self.old, self.root / 'run', budget=budget,
                                    seconds=120, cores=2, policy=policy,
                                    evaluation_dir=self.root / 'unused_evaluations', **kwargs)
            self.assertEqual(evaluator.call_count, result['logical_calls'])
        return result

    def test_budget_charges_cache_hits_and_failed_evaluations(self):
        values = [plan(10 * i) for i in range(1, 7)]
        self.legacy_batches = {
            0: [candidate('L' + str(i), value) for i, value in enumerate(values[:3])],
            1: [candidate('L' + str(i + 3), value) for i, value in enumerate(values[3:])],
        }
        self.outcomes = {
            controller.exact(values[0]): {'cache_hit': True, 'cost': 7},
            controller.exact(values[1]): {'status': 'timeout', 'cost': 3},
            controller.exact(values[2]): {'cache_hit': True, 'makespan': 90, 'cost': 5},
            controller.exact(values[3]): {'status': 'runtime_error', 'cost': 2},
        }
        result = self.run_controller(budget=4)
        self.assertEqual(result['logical_calls'], 4)
        self.assertEqual(result['new_calls'], 2)
        self.assertEqual(result['after'], 90)
        self.assertEqual([c['record']['status'] for c in result['calls']],
                         ['success', 'timeout', 'success', 'runtime_error'])
        self.assertEqual(result['stats']['legacy']['estimated_seconds'], 17)
        self.assertEqual(self.evaluated, values[:4])

    def test_cross_arm_dedup_preserves_mapping_and_schedule_order(self):
        shared = plan(10)
        key_reordered = copy.deepcopy(shared)
        key_reordered['node_to_subgraph'] = dict(reversed(list(shared['node_to_subgraph'].items())))
        schedule_reordered = copy.deepcopy(shared)
        schedule_reordered['core_schedules'][0].reverse()
        # Dict equality alone would incorrectly merge this distinct ordered encoding.
        self.assertEqual(shared, key_reordered)
        self.legacy_batches = {0: [candidate('legacy_shared', shared)]}
        self.read_batches = [[candidate('read_duplicate', shared),
                              candidate('mapping_order', key_reordered),
                              candidate('schedule_order', schedule_reordered)]]
        result = self.run_controller(budget=6, policy='interleave')
        self.assertEqual(result['logical_calls'], 3)
        self.assertEqual([controller.exact(p) for p in self.evaluated],
                         [controller.exact(p) for p in (shared, key_reordered, schedule_reordered)])
        skips = [r for r in result['routing'] if r['event'] == 'duplicate_skip']
        self.assertTrue(any(r['name'] == 'read_duplicate' for r in skips))

    def test_prior_pipeline_signatures_skip_calls_without_mutating_caller_set(self):
        previously_tried, fresh = plan(10), plan(20)
        self.legacy_batches = {0: [candidate('previously_tried', previously_tried),
                                   candidate('fresh', fresh)]}
        previous_signatures = {controller.exact(previously_tried)}
        preserved = set(previous_signatures)
        result = self.run_controller(budget=4, seen_signatures=previous_signatures)
        self.assertEqual(result['logical_calls'], 1)
        self.assertEqual(self.evaluated, [fresh])
        self.assertEqual(previous_signatures, preserved)
        self.assertTrue(any(r['event'] == 'duplicate_skip' and r['name'] == 'previously_tried'
                            for r in result['routing']))

    def test_legacy_round_two_and_exhausted_read_arm_return_budget(self):
        values = [plan(10 * i) for i in range(1, 8)]
        discarded = plan(200)
        self.legacy_batches = {
            0: [candidate('control', self.initial, control=True)] +
               [candidate('L' + str(i + 1), p) for i, p in enumerate(values[:3])] +
               [candidate('discarded_round_zero_tail', discarded)],
            1: [candidate('L' + str(i + 4), p) for i, p in enumerate(values[3:])],
        }
        self.read_batches = [[candidate('read_only', plan(100))]]
        result = self.run_controller(budget=8)
        self.assertEqual(result['logical_calls'], 8)
        self.assertEqual(result['stats']['legacy']['calls'], 7)
        self.assertEqual(result['stats']['read_order']['calls'], 1)
        self.assertEqual(self.legacy_rounds, [0, 1])
        self.assertNotIn(controller.exact(discarded), [controller.exact(p) for p in self.evaluated])
        self.assertEqual([c['name'] for c in result['calls']],
                         ['L1', 'L2', 'L3', 'L4', 'read_only', 'L5', 'L6', 'L7'])

    def test_feedback_refresh_uses_new_best_and_stops_at_two_generations(self):
        first, second, third, stale = (plan(i) for i in (10, 20, 30, 40))
        self.read_batches = [[candidate('first', first), candidate('stale', stale)],
                             [candidate('second', second), candidate('third', third)]]
        self.outcomes = {
            controller.exact(first): {'makespan': 90},
            controller.exact(second): {'makespan': 80},
            controller.exact(third): {'makespan': 70},
            controller.exact(stale): {'makespan': 95},
        }
        result = self.run_controller(budget=8)
        self.assertEqual(result['after'], 70)
        self.assertEqual(len(self.read_sources), 2)
        self.assertEqual(self.read_sources[0][0], self.initial)
        self.assertEqual(self.read_sources[1][0], first)
        self.assertEqual(self.read_sources[1][1]['source'], 'evaluation_1')
        self.assertEqual([c['name'] for c in result['calls']], ['first', 'second', 'third', 'stale'])
        read_generations = [g for g in result['generations'] if g['arm'] == 'read_order']
        self.assertEqual([g['source_makespan'] for g in read_generations], [100, 90])
        self.assertEqual(read_generations[1]['reason'], 'accepted_gain_trace_refresh')

    def test_interleave_does_not_refresh_even_after_large_improvements(self):
        first, second = plan(10), plan(20)
        self.read_batches = [[candidate('first', first), candidate('second', second)],
                             [candidate('must_not_generate', plan(30))]]
        self.outcomes = {controller.exact(first): {'makespan': 90},
                         controller.exact(second): {'makespan': 80}}
        result = self.run_controller(budget=8, policy='interleave')
        self.assertEqual(result['after'], 80)
        self.assertEqual(len(self.read_sources), 1)
        self.assertEqual(result['logical_calls'], 2)
        self.assertEqual([c['name'] for c in result['calls']], ['first', 'second'])


if __name__ == '__main__':
    unittest.main()
