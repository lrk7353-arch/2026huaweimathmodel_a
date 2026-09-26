"""Regression checks for protected feasibility time and incumbent retention."""
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import p1_incumbent as seed
import cold_portfolio as cold
from common_run import atomic_json, read_json, validate_plan
from test_p1_task_refine import graph


class IncumbentTests(unittest.TestCase):
    def test_bounded_topological_tasks_and_requested_core_coverage(self):
        ir=graph(300,[(i,i+1) for i in range(299)])
        for n in (1,5):
            for size in (32,128):
                plan=seed.safety_plan(ir,n,size)
                validate_plan(ir,plan)
                self.assertEqual(len(plan['core_schedules']),n)
                self.assertEqual(len(plan['node_to_subgraph']),300)
                self.assertLessEqual(max(Counter(plan['node_to_subgraph'].values()).values()),size)

    def exercise(self, outcomes, budget=6):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name);ir=graph(130,[(i,i+1) for i in range(129)])
        clock=[0.];limits=[]
        def evaluate(path, plan, problem, directory, timeout, **kwargs):
            limits.append(timeout)
            outcome=outcomes[len(limits)-1]
            clock[0]+=timeout if outcome=='timeout' else 2.
            p=root/f'plan{len(limits)}.json';atomic_json(p,plan)
            return dict(status=outcome,problem=1,graph_path='case_001.json',cache_hit=False,
                metrics=dict(num_cores=2,makespan=100,data_movement_bytes=dict(added_copy_bytes=0)),
                plan_path=str(p),record_path=str(root/f'record{len(limits)}.json'))
        with patch.object(seed.GraphIR,'from_path',return_value=ir), \
             patch.object(seed.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(seed,'evaluate',side_effect=evaluate), \
             patch('common_run.known',side_effect=AssertionError('history forbidden')):
            result=seed.run('case_001',2,root/'search/prefix',budget,240,60,root/'eval')
        return result,limits,root

    def test_primary_timeout_leaves_sixty_seconds_and_a_call_for_fallback(self):
        result,limits,root=self.exercise(['timeout','success'])
        self.assertEqual(limits,[120.,60.])
        self.assertEqual(result['logical_calls'],2)
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['first_incumbent_seconds'],122.)
        self.assertEqual(read_json(root/'search/incumbent.record.json'),result['best_record'])

    def test_first_success_returns_immediately_with_unspent_budget(self):
        result,limits,_=self.exercise(['success'])
        self.assertEqual(result['logical_calls'],1)
        self.assertEqual(result['elapsed_seconds'],2.)
        self.assertEqual(len(limits),1)

    def test_all_timeouts_are_not_reported_as_a_legal_incumbent(self):
        result,limits,_=self.exercise(['timeout','timeout'])
        self.assertIsNone(result['best_record'])
        self.assertEqual(result['status'],'incumbent_unverified')
        self.assertEqual(result['elapsed_seconds'],180.)

    def test_single_call_budget_is_not_overspent(self):
        result,limits,_=self.exercise(['timeout'],budget=1)
        self.assertEqual(limits,[180.])
        self.assertEqual(result['logical_calls'],1)

    def test_no_p1_optimization_if_initialization_failed(self):
        with tempfile.TemporaryDirectory() as name:
            prefix=dict(calls=[],logical_calls=0,elapsed_seconds=1,best_record=None)
            with patch.object(cold,'prefix_run',return_value=prefix), \
                 patch.object(cold.GraphIR,'from_path',side_effect=AssertionError('no optimization')), \
                 patch.object(cold,'generate_component_candidates',side_effect=AssertionError('no candidates')):
                result=cold.run('case_001',1,2,'integrated',Path(name)/'run',24,240,60)
            self.assertEqual(result['status'],'incumbent_unverified')
            self.assertIsNone(result['best_record'])


if __name__=='__main__':unittest.main()
