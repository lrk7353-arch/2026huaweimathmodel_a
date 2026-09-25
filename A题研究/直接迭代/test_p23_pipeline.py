"""From-scratch budget/provenance contracts, with no official evaluations."""
import copy
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import p23_pipeline as pipeline


def plan(label):
    return {'node_to_subgraph': {'10': label, '20': label + 1},
            'core_schedules': [[label, label + 1], []]}


def candidate(name, value):
    return {'name': name, 'plan': copy.deepcopy(value), 'metadata': {}}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.records = 0
        self.run_number = 0
        self.components, self.operations, self.wcc, self.trace = [], [], [], []
        self.outcomes = {}
        self.evaluated, self.wcc_sources, self.trace_sources, self.child_requests = [], [], [], []
        self.child_trials = []
        self.on_evaluate = None
        self.on_component_generation = None

    def record(self, value, problem, makespan=100, status='success', cache_hit=False):
        self.records += 1
        directory = self.root / ('record_' + str(self.records))
        directory.mkdir()
        plan_path = directory / 'plan.json'
        plan_path.write_text(json.dumps(value), encoding='utf-8')
        result_path = directory / 'result.json.gz'
        with gzip.open(result_path, 'wt', encoding='utf-8') as stream:
            json.dump({'record_number': self.records}, stream)
        record = dict(status=status, cache_hit=cache_hit, problem=problem,
                      graph_path=str(self.root / 'case_001.json'),
                      record_path=str(directory / 'record.json'),
                      plan_path=str(plan_path), result_path=str(result_path),
                      metrics=({'num_cores': 2, 'makespan': makespan,
                                'data_movement_bytes': {'added_copy_bytes': 10}}
                               if status == 'success' else {}))
        (directory / 'record.json').write_text(json.dumps(record), encoding='utf-8')
        return record

    def evaluate(self, graph, value, problem, directory, **kwargs):
        self.evaluated.append(dict(plan=copy.deepcopy(value), problem=problem,
                                   directory=directory, timeout=kwargs['timeout']))
        self.assertGreater(kwargs['timeout'], 0)
        self.assertLessEqual(kwargs['timeout'], 60)
        if self.on_evaluate:
            self.on_evaluate()
        outcome = self.outcomes.get(pipeline.exact(value), {})
        if outcome.get('exception'):
            raise RuntimeError('synthetic evaluator wrapper failure')
        return self.record(value, problem, **outcome)

    def component_generator(self, *args, **kwargs):
        if self.on_component_generation:
            self.on_component_generation()
        return copy.deepcopy(self.components), {}

    def wcc_generator(self, ir, value, **kwargs):
        self.wcc_sources.append(copy.deepcopy(value))
        return copy.deepcopy(self.wcc), {}

    def trace_generator(self, ir, value, raw, **kwargs):
        self.trace_sources.append(copy.deepcopy(value))
        return copy.deepcopy(self.trace), {}

    def cache_run(self, *args, **kwargs):
        self.child_requests.append((args, dict(kwargs, seen_signatures=set(kwargs['seen_signatures']))))
        best = args[1]
        trials = []
        for index, (value, outcome) in enumerate(self.child_trials):
            record = self.record(value, 3, **outcome)
            accepted = record['status'] == 'success' and pipeline.score(record) < pipeline.score(best)
            if accepted:
                best = record
            trials.append(dict(name='child_' + str(index), record=record, accepted=accepted))
        return dict(calls=trials, logical_calls=len(trials), best_record=best,
                    stop_reason='budget_or_candidates')

    def run_pipeline(self, problem=2, budget=12, seconds=120, refinement='legacy', method='staged', proposal_budget=None):
        self.run_number += 1
        out = self.root / ('run_' + str(self.run_number))
        with patch.object(pipeline.GraphIR, 'from_path', return_value=object()), \
             patch.object(pipeline, 'validate_plan', return_value=True), \
             patch.object(pipeline, 'generate_component_candidates', side_effect=self.component_generator), \
             patch.object(pipeline, 'generate_wcc_candidates', side_effect=self.wcc_generator) as wcc_mock, \
             patch.object(pipeline, 'generate_operation_candidates', side_effect=lambda *a, **k: (copy.deepcopy(self.operations), {})) as operation_mock, \
             patch.object(pipeline, 'generate_trace_candidates', side_effect=self.trace_generator), \
             patch.object(pipeline, 'evaluate', side_effect=self.evaluate), \
             patch('run_p3_refine.run', side_effect=self.cache_run), \
             patch('p3_feedback.run', side_effect=self.cache_run):
            result = pipeline.run('case_001', problem, 2, method, out,
                                  budget=budget, seconds=seconds, evaluation_timeout=60,
                                  evaluation_dir=self.root / 'fresh_shared_evaluations',
                                  p3_refinement=refinement, proposal_budget=proposal_budget)
            self.last_wcc_calls = wcc_mock.call_count
            self.last_wcc_kwargs = wcc_mock.call_args.kwargs if wcc_mock.call_args else None
            self.last_operation_calls = operation_mock.call_count
        return result

    def test_wider_proposal_pool_does_not_raise_evaluation_cap(self):
        self.components=[candidate('C1',plan(10)),candidate('C2',plan(20))]
        self.wcc=[candidate('W'+str(i),plan(i*10)) for i in range(3,9)]
        result=self.run_pipeline(method='component_wcc',budget=3,proposal_budget=12)
        self.assertEqual(self.last_wcc_kwargs['max_candidates'],12)
        self.assertEqual(result['logical_calls'],3)
        self.assertEqual(result['proposal_budget'],12)

    def test_routed_component_search_keeps_budget_and_does_not_enter_operations(self):
        self.components = [candidate('C1',plan(10)),candidate('C2',plan(20))]
        self.wcc = [candidate('W'+str(i),plan(100+i*10)) for i in range(6)]
        self.operations = [candidate('O',plan(300))]
        with patch.object(pipeline,'structural_route',return_value={'route':'component_wcc'}):
            result=self.run_pipeline(budget=6,method='routed')
        self.assertEqual(result['logical_calls'],6)
        self.assertEqual(result['effective_method'],'component_wcc')
        self.assertEqual(self.last_operation_calls,0)
        self.assertTrue(all(c['phase'] in ('component','wcc') for c in result['calls']))

    def test_observed_probe_is_charged_and_cannot_steal_more_than_total_budget(self):
        self.components = [candidate('C1', plan(10)), candidate('C2', plan(20))]
        self.wcc = [candidate('W1', plan(30)), candidate('W2', plan(40)), candidate('late', plan(50))]
        self.wcc[-1]['metadata'] = dict(strategy='window_interleave', per_core_proxy=[
            dict(compute_only_predicted_end=50, compute_touch_live_bytes_proxy={})])
        self.operations = [candidate('O1', plan(60)), candidate('O2', plan(70))]
        self.outcomes[pipeline.exact(plan(50))] = dict(status='timeout')
        with patch.object(pipeline, 'structural_route', return_value={'route': 'component_wcc'}), \
             patch.object(pipeline, 'observed_route', return_value={'route': 'staged', 'probe': True}):
            result = self.run_pipeline(problem=3, budget=6, method='trace_routed')
        self.assertEqual(result['logical_calls'], 6)
        self.assertEqual([t['phase'] for t in result['calls']],
                         ['component', 'component', 'wcc', 'wcc', 'wcc_probe', 'operation'])
        self.assertEqual(result['routing']['decision_after_calls'], 4)
        self.assertEqual(result['routing']['probe_selection']['name'], 'late')
        self.assertEqual(self.child_requests, [])

    def test_observed_bad_timeline_falls_back_to_staged_without_extra_call(self):
        self.components = [candidate('C', plan(10))]
        self.operations = [candidate('O', plan(20))]
        with patch.object(pipeline, 'structural_route', return_value={'route': 'component_wcc'}), \
             patch.object(pipeline, 'observed_route', side_effect=ValueError('bad identity')):
            result = self.run_pipeline(budget=4, method='trace_routed')
        self.assertEqual(result['effective_method'], 'staged')
        self.assertEqual(result['routing']['reason'], 'unusable_timeline')
        self.assertEqual(result['logical_calls'], 2)

    def test_observed_wcc_decision_preserves_late_candidates(self):
        self.components = [candidate('C1', plan(10)), candidate('C2', plan(20))]
        self.wcc = [candidate('W'+str(i), plan(100+i*10)) for i in range(6)]
        self.outcomes[pipeline.exact(plan(150))] = dict(makespan=40)
        with patch.object(pipeline, 'structural_route', return_value={'route': 'component_wcc'}), \
             patch.object(pipeline, 'observed_route', return_value={'route': 'component_wcc', 'probe': False}):
            result = self.run_pipeline(budget=8, method='trace_routed')
        self.assertEqual(result['logical_calls'], 8)
        self.assertEqual(pipeline.score(result['best_record'])[0], 40)
        self.assertEqual(self.last_operation_calls, 0)

    def test_routed_staged_search_can_recover_failed_components(self):
        c,op=plan(10),plan(20)
        self.components=[candidate('C',c)]
        self.operations=[candidate('O',op)]
        self.outcomes={pipeline.exact(c):dict(status='timeout'),pipeline.exact(op):dict(makespan=60)}
        with patch.object(pipeline,'structural_route',return_value={'route':'staged'}):
            result=self.run_pipeline(budget=4,method='routed')
        self.assertEqual(result['effective_method'],'staged')
        self.assertEqual(pipeline.score(result['best_record'])[0],60)
        self.assertEqual(result['logical_calls'],2)

    def test_route_uses_component_work_and_is_invariant_to_uniform_scaling(self):
        def ir(work):
            return SimpleNamespace(components=[SimpleNamespace(work_m=m,work_v=v,work_other=0) for m,v in work])
        balanced=[(100,20)]*10
        heavy=[(900,200),(10,5),(10,5)]
        for work,expected in [(balanced,'component_wcc'),(heavy,'staged')]:
            a=pipeline.structural_route(ir(work),5)
            b=pipeline.structural_route(ir([(7*m,7*v) for m,v in work]),5)
            self.assertEqual(a['route'],expected)
            self.assertEqual(b['route'],expected)
            self.assertAlmostEqual(a['component_to_ideal_ratio'],b['component_to_ideal_ratio'])

    def test_initial_failures_wrapper_exceptions_and_cache_hits_share_budget(self):
        c1, c2, w1, o1, o2, t1, t2 = [plan(i * 10) for i in range(1, 8)]
        self.components = [candidate('C1', c1), candidate('C2', c2)]
        self.wcc = [candidate('W1', w1)]
        self.operations = [candidate('O1', o1), candidate('O2', o2)]
        self.trace = [candidate('T1', t1), candidate('T2', t2)]
        self.outcomes = {
            pipeline.exact(c1): dict(cache_hit=True),
            pipeline.exact(c2): dict(status='timeout'),
            pipeline.exact(w1): dict(status='runtime_error'),
            pipeline.exact(o1): dict(exception=True),
            pipeline.exact(o2): dict(makespan=90),
            pipeline.exact(t1): dict(makespan=80, cache_hit=True),
            pipeline.exact(t2): dict(status='timeout'),
        }
        result = self.run_pipeline(problem=3, budget=7)
        self.assertEqual(result['logical_calls'], 7)
        self.assertEqual(result['new_calls'], 5)
        self.assertEqual(len(self.evaluated), 7)
        self.assertEqual(pipeline.score(result['best_record'])[0], 80)
        self.assertEqual([c['phase'] for c in result['calls']],
                         ['component', 'component', 'wcc', 'operation', 'operation', 'trace', 'trace'])
        self.assertEqual(result['calls'][3]['record']['status'], 'wrapper_exception')
        self.assertEqual(self.child_requests, [])
        self.assertEqual(result['stop_reason'], 'call_budget')

    def test_failed_components_do_not_block_operation_feasibility(self):
        c1, c2, operation = plan(10), plan(20), plan(30)
        self.components = [candidate('C1', c1), candidate('C2', c2)]
        self.operations = [candidate('O', operation)]
        self.outcomes = {pipeline.exact(c1): dict(status='timeout'),
                         pipeline.exact(c2): dict(status='runtime_error'),
                         pipeline.exact(operation): dict(makespan=70)}
        result = self.run_pipeline(budget=4)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['logical_calls'], 3)
        self.assertIsNone(result['best_component'])
        self.assertEqual(pipeline.score(result['best_record'])[0], 70)
        self.assertEqual(self.last_wcc_calls, 0)
        self.assertEqual(self.last_operation_calls, 1)
        self.assertTrue(all(p == operation for p in self.trace_sources))

    def test_wcc_uses_component_parent_even_after_better_operation_plan(self):
        c1, c2, c3, operation = plan(10), plan(20), plan(30), plan(40)
        self.components = [candidate('C1', c1), candidate('C2', c2), candidate('C3', c3)]
        self.operations = [candidate('O', operation)]
        self.outcomes = {pipeline.exact(c1): dict(makespan=100),
                         pipeline.exact(c2): dict(makespan=110),
                         pipeline.exact(c3): dict(makespan=90),
                         pipeline.exact(operation): dict(makespan=50)}
        result = self.run_pipeline(budget=6)
        self.assertEqual(self.wcc_sources, [c1, c3])
        self.assertEqual(pipeline.score(result['best_component'])[0], 90)
        self.assertEqual(pipeline.score(result['best_record'])[0], 50)
        self.assertTrue(all(p != operation for p in self.wcc_sources))

    def test_cache_inherits_only_remaining_budget_eval_directory_and_seen_plans(self):
        c1, c2, operation, trace, child_success, child_failure = [plan(i * 10) for i in range(1, 7)]
        self.components = [candidate('C1', c1), candidate('C2', c2),
                           candidate('later_duplicate_success', child_success),
                           candidate('later_duplicate_failure', child_failure)]
        self.operations = [candidate('O', operation)]
        self.trace = [candidate('T', trace)]
        self.outcomes = {pipeline.exact(c1): dict(makespan=100),
                         pipeline.exact(c2): dict(makespan=110, cache_hit=True),
                         pipeline.exact(operation): dict(makespan=90),
                         pipeline.exact(trace): dict(makespan=80)}
        self.child_trials = [(child_success, dict(makespan=60, cache_hit=True)),
                             (child_failure, dict(status='timeout'))]
        result = self.run_pipeline(problem=3, budget=8, refinement='feedback')
        self.assertEqual(len(self.child_requests), 1)
        args, kwargs = self.child_requests[0]
        self.assertEqual(kwargs['budget'], 4)
        self.assertGreater(kwargs['seconds'], 0)
        self.assertLessEqual(kwargs['seconds'], 120)
        self.assertEqual(kwargs['evaluation_dir'], self.root / 'fresh_shared_evaluations')
        self.assertEqual(kwargs['seen_signatures'], {pipeline.exact(v) for v in (c1, c2, operation, trace)})
        self.assertEqual(kwargs['policy'], 'feedback')
        self.assertEqual(pipeline.score(args[1])[0], 80)
        self.assertTrue(all(e['directory'] == kwargs['evaluation_dir'] for e in self.evaluated))
        self.assertEqual(result['logical_calls'], 6)
        self.assertEqual(result['new_calls'], 4)
        self.assertEqual(pipeline.score(result['best_record'])[0], 60)
        # Success and failed child proposals alike must not be re-evaluated by fallback.
        self.assertEqual(len(self.evaluated), 4)
        self.assertTrue({'later_duplicate_success', 'later_duplicate_failure'} <=
                        {s['name'] for s in result['skipped']})

    def test_legacy_child_receives_same_eval_directory_and_timeout(self):
        self.components = [candidate('C1', plan(10))]
        result = self.run_pipeline(problem=3, budget=4, refinement='legacy')
        args, kwargs = self.child_requests[0]
        self.assertIsNone(args[2])
        self.assertEqual(kwargs['budget'], 3)
        self.assertEqual(kwargs['per_call_timeout'], 60)
        self.assertEqual(kwargs['evaluation_dir'], self.root / 'fresh_shared_evaluations')
        self.assertEqual(kwargs['seen_signatures'], {pipeline.exact(plan(10))})
        self.assertEqual(result['logical_calls'], 1)

    def test_all_failed_candidates_return_no_best_record(self):
        c, operation = plan(10), plan(20)
        self.components = [candidate('C', c)]
        self.operations = [candidate('O', operation)]
        self.outcomes = {pipeline.exact(c): dict(status='timeout'),
                         pipeline.exact(operation): dict(exception=True)}
        result = self.run_pipeline(problem=3, budget=4)
        self.assertEqual(result['status'], 'no_feasible_result')
        self.assertIsNone(result['best_record'])
        self.assertEqual(result['logical_calls'], 2)
        self.assertEqual(self.child_requests, [])
        self.assertEqual(self.trace_sources, [])
        self.assertFalse((self.root / 'run_1' / 'best.plan.json').exists())

    def test_deadline_stops_new_calls_after_evaluation(self):
        clock = [0.0]
        self.components = [candidate('C1', plan(10)), candidate('C2', plan(20))]
        self.on_evaluate = lambda: clock.__setitem__(0, 2.0)
        with patch.object(pipeline.time, 'monotonic', side_effect=lambda: clock[0]):
            result = self.run_pipeline(problem=3, budget=8, seconds=1)
        self.assertEqual(result['logical_calls'], 1)
        self.assertEqual(self.evaluated[0]['timeout'], 1)
        self.assertEqual(result['stop_reason'], 'time_budget')
        self.assertEqual(self.last_operation_calls, 0)
        self.assertEqual(self.child_requests, [])

    def test_candidate_generation_time_is_inside_deadline(self):
        clock = [0.0]
        self.components = [candidate('C1', plan(10))]
        self.on_component_generation = lambda: clock.__setitem__(0, 2.0)
        with patch.object(pipeline.time, 'monotonic', side_effect=lambda: clock[0]):
            result = self.run_pipeline(problem=3, budget=8, seconds=1)
        self.assertEqual(result['logical_calls'], 0)
        self.assertEqual(result['stop_reason'], 'time_budget')
        self.assertIsNone(result['best_record'])
        self.assertEqual(self.last_operation_calls, 0)


if __name__ == '__main__':
    unittest.main()
