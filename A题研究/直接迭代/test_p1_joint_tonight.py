"""Protocol counterexamples with no official evaluator calls."""
import gzip
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import run_p1_joint_tonight as run


class EveningProtocol(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ir = SimpleNamespace(path=self.root/'graph.json', compute_ids=(1,))
        self.plan = dict(node_to_subgraph={'1': 0}, core_schedules=[[0], [], [], [], []])
        self.proposed = dict(node_to_subgraph={'1': 1}, core_schedules=[[1], [], [], [], []])
        self.record = dict(status='success', metrics=dict(makespan=100, num_cores=5,
            data_movement_bytes=dict(added_copy_bytes=10), memory_peak_by_core={'0': {'UB': 40}}),
            plan_path=str(self.root/'plan.json'))
        run.atomic_json(self.root/'plan.json', self.plan)
        raw = dict(makespan=100, num_cores=5, per_core_timeline=[dict(core_id=i,
            tasks=[{'task_id': 0}] if i==0 else [],
            ops=[{'op_id': 1, 'task_id': 0}] if i==0 else []) for i in range(5)])
        with gzip.open(self.root/'trace.gz', 'wt') as handle:
            json.dump(raw, handle)
        self.seed = dict(case='case_047', plan_path=str(self.root/'plan.json'),
            trace_path=str(self.root/'trace.gz'), record=self.record, expected_score=[100, 10])
        self.protocol = dict(generation_seconds=1, arm_seconds=5, worker_seconds=2, version=1, max_candidates=2)

    def job(self):
        return self.seed, 'beam_small', str(self.root/'arm'), self.protocol, time.time()+10

    def context(self, candidates, evaluate):
        return patch.multiple(run, warm_modules=lambda: 0,
            GraphIR=SimpleNamespace(from_path=lambda _: self.ir), validate_plan=lambda *_: None,
            candidates=candidates, evaluate=evaluate)

    def test_generation_failure_is_not_a_tie_or_a_paid_call(self):
        def generate(*_): raise TimeoutError('model construction exceeded deadline')
        def forbidden(*_, **__): self.fail('generation failure cannot cause an official call')
        with self.context(generate, forbidden): state = run.run_arm(self.job())
        self.assertTrue(state['complete'])
        self.assertEqual(state['stop_reason'], 'generation_error')
        self.assertEqual(state['calls'], [])
        self.assertEqual(state['errors'][0]['type'], 'TimeoutError')

    def test_paid_pending_is_recovered_once_and_updates_best(self):
        out = self.root/'arm'
        paid = dict(self.record, metrics=dict(self.record['metrics'], makespan=90), elapsed_seconds=.1)
        run.atomic_json(out/'evaluations/call_01/attempts/one/record.json', paid)
        pending = dict(index=0, name='candidate', evaluation_dir='evaluations/call_01',
            elapsed_at_start=.2, timeout=2, metadata={'proxy': 80})
        state = dict(case='case_047', method='beam_small', protocol=self.protocol,
            seed_score=[100, 10], seed_record=self.record, complete=False, generation_complete=True,
            candidates=[dict(index=0)], calls=[], errors=[], best_record=self.record,
            elapsed_seconds=.3, pending=pending)
        run.atomic_json(out/'summary.json', state)
        def forbidden(*_, **__): self.fail('recovery must not evaluate again')
        with self.context(forbidden, forbidden):
            first = run.run_arm(self.job())
            second = run.run_arm(self.job())
        self.assertEqual(len(first['calls']), 1)
        self.assertEqual(run.score(first['best_record']), (90, 10))
        self.assertEqual(first, second)

    def test_missing_pending_attempt_consumes_budget(self):
        state = dict(calls=[], elapsed_seconds=1,
            pending=dict(index=0, evaluation_dir='missing', elapsed_at_start=1, timeout=2))
        run.recover_pending(state, self.root)
        self.assertEqual(len(state['calls']), 1)
        self.assertEqual(state['calls'][0]['record']['status'], 'interrupted')
        self.assertEqual(state['elapsed_seconds'], 3)

    def test_validation_precedes_official_evaluation(self):
        self.seed['expected_score'] = [101, 10]
        raw_path = self.root/'trace.gz'
        with gzip.open(raw_path, 'rt') as handle: raw = json.load(handle)
        raw['per_core_timeline'][0]['ops'][0]['task_id'] = 7
        with gzip.open(raw_path, 'wt') as handle: json.dump(raw, handle)
        def forbidden(*_, **__): self.fail('stale trace should stop before generation or payment')
        with self.context(forbidden, forbidden), self.assertRaisesRegex(ValueError, 'ownership'):
            run.run_arm(self.job())

    def test_deterministic_metrics_keep_simulated_memory(self):
        a = dict(self.record, metrics=dict(self.record['metrics'], peak_memory_bytes=1000))
        b = dict(self.record, metrics=dict(self.record['metrics'], peak_memory_bytes=2000))
        self.assertEqual(run.deterministic_metrics(a), run.deterministic_metrics(b))
        b['metrics']['memory_peak_by_core'] = {'0': {'UB': 41}}
        self.assertNotEqual(run.deterministic_metrics(a), run.deterministic_metrics(b))

    def test_intact_noop_preserves_exact_original_encoding(self):
        from test_p1_task_refine import graph
        ir = graph(3, [])
        original = dict(node_to_subgraph={'2': 19, '0': 7, '1': 7}, core_schedules=[[7], [19]])
        relabelled = dict(node_to_subgraph={'2': 1, '0': 0, '1': 0}, core_schedules=[[0], [1]])
        restored = run.preserve_intact_task_ids(ir, original, relabelled)
        self.assertEqual(run.exact_signature(restored), run.exact_signature(original))
        moved = dict(relabelled, core_schedules=[[1], [0]])
        self.assertEqual(run.preserve_intact_task_ids(ir, original, moved)['core_schedules'], [[19], [7]])

    def test_intact_restoration_rejects_hidden_split_or_merge(self):
        from test_p1_task_refine import graph
        ir = graph(3, [])
        original = dict(node_to_subgraph={'0': 7, '1': 7, '2': 19}, core_schedules=[[7], [19]])
        split = dict(node_to_subgraph={'0': 0, '1': 1, '2': 2}, core_schedules=[[0, 1], [2]])
        merged = dict(node_to_subgraph={'0': 0, '1': 0, '2': 0}, core_schedules=[[0], []])
        for invalid in (split, merged):
            with self.assertRaises(ValueError): run.preserve_intact_task_ids(ir, original, invalid)


if __name__ == '__main__': unittest.main()
