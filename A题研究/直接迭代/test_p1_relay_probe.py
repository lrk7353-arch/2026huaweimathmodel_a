"""Small protocol counterexamples; all official evaluations are mocked."""
import gzip
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import run_p1_relay_probe as relay


class RelayProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ir = SimpleNamespace(path=self.root/'case.json', compute_ids=(1,))
        self.seed_record = self.record('seed', 100, 0)
        self.seed = dict(id='case047_region', case='case_047', kind='region_mid',
                         plan_path=self.seed_record['plan_path'], expected_makespan=100,
                         expected_added_copy_bytes=10, selected_makespan=70,
                         selected_added_copy_bytes=10)
        self.seed_result = dict(seed=self.seed, record=self.seed_record, verified=True)

    @staticmethod
    def plan(task):
        return dict(node_to_subgraph={'1': task}, core_schedules=[[task], [], [], [], []])

    def record(self, name, makespan, task, elapsed=2.):
        base = self.root/name
        base.mkdir(parents=True, exist_ok=True)
        plan = self.plan(task)
        (base/'plan.json').write_text(json.dumps(plan))
        raw = dict(makespan=makespan, num_cores=5, per_core_timeline=[
            dict(core_id=core, tasks=[dict(task_id=t) for t in tasks],
                 ops=[dict(op_id=1, task_id=task)] if core == 0 else [])
            for core, tasks in enumerate(plan['core_schedules'])])
        with gzip.open(base/'raw.json.gz', 'wt') as stream:
            json.dump(raw, stream)
        record = dict(status='success', metrics=dict(makespan=makespan, num_cores=5,
                      data_movement_bytes=dict(added_copy_bytes=10)),
                      plan_path=str(base/'plan.json'), result_path=str(base/'raw.json.gz'),
                      record_path=str(base/'record.json'), elapsed_seconds=elapsed,
                      cache_hit=False)
        (base/'record.json').write_text(json.dumps(record))
        return record

    def context(self, proposals, evaluate):
        return patch.multiple(relay, GraphIR=SimpleNamespace(from_path=lambda _: self.ir),
                              validate_plan=lambda *_: None, proposals=proposals,
                              evaluate=evaluate)

    def test_trace_rejects_same_task_ids_with_wrong_compute_owner(self):
        plan = self.plan(0)
        with gzip.open(self.seed_record['result_path'], 'rt') as stream:
            raw = json.load(stream)
        raw['per_core_timeline'][0]['ops'][0]['task_id'] = 8
        with self.assertRaisesRegex(ValueError, 'ownership'):
            relay.assert_trace(self.ir, plan, raw, self.seed_record)

    def test_pending_finished_call_is_charged_once_and_updates_best(self):
        out = self.root/'arm'
        target = out/'evaluations/call_001/attempts/one'
        target.mkdir(parents=True)
        improved = self.record('improved', 90, 1, elapsed=5.)
        (target/'record.json').write_text(json.dumps(improved))
        state = dict(best_record=self.seed_record, calls=[], elapsed_seconds=8.,
                     active_stream=dict(round=0, parent_record=self.seed_record['record_path']),
                     pending=dict(evaluation_dir='evaluations/call_001', timeout=60.,
                                  active_elapsed_at_start=3., signature='unique'))
        relay._recover_pending(state, out)
        self.assertEqual(state['elapsed_seconds'], 8.)
        self.assertEqual(len(state['calls']), 1)
        self.assertTrue(state['calls'][0]['accepted'])
        self.assertEqual(state['best_record']['metrics']['makespan'], 90)
        self.assertFalse(state.get('active_stream'))
        relay._recover_pending(state, out)
        self.assertEqual(len(state['calls']), 1)
        self.assertEqual(state['elapsed_seconds'], 8.)

    def test_pending_missing_record_consumes_slot_without_free_retry(self):
        state = dict(best_record=self.seed_record, calls=[], elapsed_seconds=4.,
                     pending=dict(evaluation_dir='evaluations/call_001', timeout=7.,
                                  active_elapsed_at_start=3., signature='unique'))
        relay._recover_pending(state, self.root/'missing')
        self.assertEqual(state['elapsed_seconds'], 10.)
        self.assertEqual(state['calls'][0]['record']['status'], 'interrupted')
        self.assertFalse(state['calls'][0]['accepted'])

    def test_empty_streams_stop_without_official_calls(self):
        rounds = []
        def proposals(*args):
            rounds.append(args[-1])
            yield from ()
        def forbidden(*args, **kwargs):
            self.fail('Empty streams must not incur official calls')
        with self.context(proposals, forbidden):
            state = relay.run_arm(self.seed_result, 'legacy_joint', self.root/'empty', seconds=10)
        self.assertTrue(state['complete'])
        self.assertEqual(state['stop_reason'], 'candidate_exhaustion')
        self.assertEqual(rounds, [0, 1, 2, 3])
        self.assertEqual(state['calls'], [])

    def test_resume_preserves_unconsumed_parent_queue(self):
        out = self.root/'resume'
        observed_rounds = []
        def proposals(ir, plan, raw, family, round_index):
            observed_rounds.append(round_index)
            if round_index == 0:
                yield dict(name='first_worse', plan=self.plan(1))
                yield dict(name='second_better', plan=self.plan(2))
        paid_tasks = []
        def evaluate(graph, plan, problem, directory, **kwargs):
            task = plan['node_to_subgraph']['1']
            paid_tasks.append(task)
            return self.record('paid_'+str(task), 110 if task == 1 else 90, task)
        original_atomic = relay.atomic_json
        interrupted = False
        def interrupt_after_first_checkpoint(path, value):
            nonlocal interrupted
            original_atomic(path, value)
            if (Path(path).name == 'summary.json' and len(value.get('calls', [])) == 1
                    and not value.get('pending') and not interrupted):
                interrupted = True
                raise KeyboardInterrupt('simulated user interruption')
        with self.context(proposals, evaluate):
            with patch.object(relay, 'atomic_json', interrupt_after_first_checkpoint):
                with self.assertRaises(KeyboardInterrupt):
                    relay.run_arm(self.seed_result, 'legacy_joint', out, budget=2, seconds=10)
            saved = json.loads((out/'summary.json').read_text())
            self.assertFalse(saved['complete'])
            self.assertEqual(saved['active_stream']['round'], 0)
            state = relay.run_arm(self.seed_result, 'legacy_joint', out, budget=2, seconds=10)
        self.assertEqual(paid_tasks, [1, 2])
        self.assertEqual(observed_rounds, [0, 0])
        self.assertEqual(state['best_record']['metrics']['makespan'], 90)
        self.assertEqual(len(state['calls']), 2)

    def test_improvement_rebuilds_from_matching_new_plan_and_trace(self):
        parents = []
        def proposals(ir, plan, raw, family, round_index):
            task = plan['node_to_subgraph']['1']
            parents.append((task, raw['makespan'], round_index))
            yield dict(name='improve', plan=self.plan(task+1))
        def evaluate(graph, plan, problem, directory, **kwargs):
            task = plan['node_to_subgraph']['1']
            return self.record('better_'+str(task), 100-10*task, task)
        with self.context(proposals, evaluate):
            state = relay.run_arm(self.seed_result, 'legacy_joint', self.root/'better', budget=2, seconds=10)
        self.assertEqual(parents, [(0, 100, 0), (1, 90, 1)])
        self.assertEqual(state['best_record']['metrics']['makespan'], 80)


if __name__ == '__main__':
    unittest.main()
