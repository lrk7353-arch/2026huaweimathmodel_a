"""Cold P1 experimental integration: shared budgets, paid parents and traces."""
from contextlib import contextmanager
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cold_portfolio as portfolio
from test_p1_task_refine import graph


def plan(index):
    base = 3*index
    return {'node_to_subgraph': {'0': base, '1': base+1, '2': base+2},
            'core_schedules': [[base, base+1], [base+2]]}


class TonightPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ir = graph(3, [])
        self.serial = 0

    def record(self, proposal, span=1000):
        self.serial += 1
        out = self.root/str(self.serial)
        out.mkdir()
        portfolio.atomic_json(out/'plan.json', proposal)
        raw = dict(makespan=span, num_cores=2, per_core_timeline=[])
        for core, tasks in enumerate(proposal['core_schedules']):
            raw['per_core_timeline'].append(dict(core_id=core,
                tasks=[dict(task_id=task) for task in tasks],
                ops=[dict(op_id=int(op), task_id=task)
                     for op, task in proposal['node_to_subgraph'].items() if task in tasks]))
        with gzip.open(out/'raw.json.gz', 'wt') as stream:
            json.dump(raw, stream)
        return dict(status='success', problem=1, graph_path='case_001.json',
                    plan_path=str(out/'plan.json'), result_path=str(out/'raw.json.gz'),
                    record_path=str(out/'record.json'), cache_hit=False,
                    metrics=dict(makespan=span, num_cores=2,
                                 data_movement_bytes=dict(added_copy_bytes=0)))

    @staticmethod
    def prefix(records, best):
        return dict(calls=[dict(record=record, name='prefix') for record in records],
                    logical_calls=len(records), elapsed_seconds=.1, best_record=best)

    def invoke(self, prefix, generator, evaluate, budget=5, seconds=60):
        with patch.object(portfolio, 'prefix_run', return_value=prefix), \
             patch.object(portfolio.GraphIR, 'from_path', return_value=self.ir), \
             patch.object(portfolio, 'local_order', return_value=(['tonight'], {})), \
             patch('run_p1_joint_tonight.candidates', side_effect=generator), \
             patch.object(portfolio, 'evaluate', side_effect=evaluate), \
             patch('common_run.known', side_effect=AssertionError('historical library forbidden')):
            return portfolio.run('case_001', 1, 2, 'joint_tonight', self.root/'run',
                                 budget=budget, seconds=seconds, evaluation_timeout=5)

    def test_variant_is_explicit_p1_only_and_default_order_unchanged(self):
        self.assertEqual(portfolio.local_order(None, None, 1, 'integrated')[0],
                         ['joint', 'structure', 'joint', 'legacy'])
        self.assertEqual(portfolio.local_order(None, None, 1, 'joint_tonight')[0],
                         ['joint', 'structure', 'tonight', 'legacy'])
        for problem in (2, 3):
            target = self.root/f'p{problem}'
            with self.assertRaisesRegex(ValueError, 'requires P1'):
                portfolio.run('case_001', problem, 2, 'joint_tonight', target)
            self.assertFalse(target.exists())

    def test_full_budget_uses_identical_six_call_prefix(self):
        records = [self.record(plan(index), 1000+index) for index in range(6)]
        prefix = self.prefix(records, records[0])
        observed = []
        for variant in ('integrated', 'joint_tonight'):
            with patch.object(portfolio, 'prefix_run', return_value=prefix) as start, \
                 patch.object(portfolio.GraphIR, 'from_path', return_value=self.ir), \
                 patch.object(portfolio, 'local_order', return_value=(['legacy'], {})), \
                 patch.object(portfolio, 'local_candidates', return_value=([], {})), \
                 patch.object(portfolio, 'evaluate', side_effect=AssertionError('no proposals')):
                result = portfolio.run('case_001', 1, 2, variant, self.root/variant,
                                       budget=24, seconds=240, evaluation_timeout=60)
            args, kwargs = start.call_args
            observed.append((args[:3], args[4], args[6], kwargs))
            self.assertEqual(result['prefix_cap'], 6)
            self.assertEqual(result['logical_calls'], 6)
            self.assertEqual(result['budget'], 24)
            self.assertEqual(result['soft_time_budget'], 240)
        self.assertEqual(observed[0], observed[1])

    def test_each_paid_parent_has_two_expansions_within_shared_call_budget(self):
        seed = self.record(plan(0))
        generated, paid = [], []
        def generate(ir, parent, raw, method, seconds):
            generated.append((method, seconds, raw['makespan']))
            index = len(generated)
            return [dict(name=method, plan=plan(index), metadata={})], {}
        def evaluate(graph_path, proposal, *args, **kwargs):
            paid.append(proposal)
            return self.record(proposal, 1100)
        result = self.invoke(self.prefix([seed], seed), generate, evaluate, budget=8)
        self.assertEqual([entry[0] for entry in generated], ['cpsat_intact', 'cpsat_warm'])
        self.assertTrue(all(0 < entry[1] <= 20 for entry in generated))
        self.assertEqual(result['logical_calls'], 3)
        self.assertEqual(len(paid), 2)
        stages = [stage for stage in result['stages'] if stage['phase']=='local_generation']
        self.assertEqual([stage['round_index'] for stage in stages], [0, 1])
        self.assertTrue(all(stage['parent_record']==seed['record_path'] for stage in stages))
        self.assertEqual([stage['diagnostics']['portfolio_method'] for stage in stages],
                         ['cpsat_intact', 'cpsat_warm'])

    def test_improved_paid_parent_refreshes_matching_plan_and_trace(self):
        seed = self.record(plan(0))
        generated, paid = [], []
        def generate(ir, parent, raw, method, seconds):
            generated.append((parent, raw['makespan'], method))
            return [dict(name=method, plan=plan(len(generated)), metadata={})], {}
        def evaluate(graph_path, proposal, *args, **kwargs):
            paid.append(self.record(proposal, 1000-100*(len(paid)+1)))
            return paid[-1]
        result = self.invoke(self.prefix([seed], seed), generate, evaluate, budget=3)
        self.assertEqual([(span, method) for _, span, method in generated],
                         [(1000, 'cpsat_intact'), (900, 'cpsat_intact')])
        self.assertEqual(generated[1][0], plan(1))
        self.assertEqual(result['calls'][2]['parent_record'], paid[0]['record_path'])
        self.assertEqual(result['logical_calls'], 3)

    def test_same_makespan_with_wrong_compute_ownership_is_rejected(self):
        seed = self.record(plan(0))
        with gzip.open(seed['result_path'], 'rt') as stream:
            raw = json.load(stream)
        raw['per_core_timeline'][0]['ops'][0]['task_id'] = 99
        with gzip.open(seed['result_path'], 'wt') as stream:
            json.dump(raw, stream)
        forbidden = lambda *args, **kwargs: self.fail('bad trace must not generate or evaluate')
        result = self.invoke(self.prefix([seed], seed), forbidden, forbidden)
        self.assertEqual(result['logical_calls'], 1)
        self.assertTrue(any('ownership' in stage.get('error', '') for stage in result['stages']))

    def test_remaining_wall_budget_limits_generation_and_blocks_late_evaluation(self):
        seed = self.record(plan(0))
        clock, requested, hard_caps = [0.], [], []
        original_prefix = self.prefix([seed], seed)
        def prefix(*args, **kwargs):
            clock[0] = 8.
            return original_prefix
        @contextmanager
        def cap(seconds):
            hard_caps.append(seconds)
            yield
        def generate(ir, parent, raw, method, seconds):
            requested.append(seconds)
            clock[0] = 11.
            return [dict(name='late', plan=plan(1), metadata={})], {}
        with patch.object(portfolio, 'prefix_run', side_effect=prefix), \
             patch.object(portfolio.GraphIR, 'from_path', return_value=self.ir), \
             patch.object(portfolio, 'local_order', return_value=(['tonight'], {})), \
             patch.object(portfolio.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch('persistent_search.generation_limit', side_effect=cap), \
             patch('run_p1_joint_tonight.candidates', side_effect=generate), \
             patch.object(portfolio, 'evaluate', side_effect=AssertionError('late evaluation forbidden')):
            result = portfolio.run('case_001', 1, 2, 'joint_tonight', self.root/'time',
                                   budget=24, seconds=10, evaluation_timeout=5)
        self.assertEqual(requested, [2.])
        self.assertTrue(hard_caps)
        self.assertTrue(all(limit == 2. for limit in hard_caps))
        self.assertEqual(result['logical_calls'], 1)
        self.assertEqual(result['stop_reason'], 'time_budget')


if __name__ == '__main__':
    unittest.main()
