"""Admission, parent provenance and bounded gain/cost allocation contracts."""
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from common_run import atomic_json
from search_budget import SearchBudget
import cold_portfolio as cp
from test_p1_task_refine import graph


def plan(kind=0, offset=0):
    schedules=[[[0,1],[2]],[[0],[1,2]],[[1],[0,2]],[[2],[0,1]]][kind]
    return dict(node_to_subgraph={str(i):i+offset for i in range(3)},
                core_schedules=[[s+offset for s in seq] for seq in schedules])


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.counter=0

    def record(self, value, span):
        self.counter+=1;out=self.root/str(self.counter);out.mkdir()
        atomic_json(out/'plan.json',value)
        with gzip.open(out/'result.gz','wt') as f:json.dump(dict(makespan=span),f)
        return dict(status='success',cache_hit=False,problem=2,graph_path='case_001.json',
            plan_path=str(out/'plan.json'),record_path=str(out/'record.json'),result_path=str(out/'result.gz'),
            metrics=dict(makespan=span,num_cores=2,data_movement_bytes=dict(added_copy_bytes=0)))

    def test_beam_is_bounded_distinct_and_rechecks_margin(self):
        a=self.record(plan(),100);alias=self.record(plan(0,10),101)
        b=self.record(plan(1),102);c=self.record(plan(2),104);far=self.record(plan(3),106)
        s=SearchBudget(3,['region','data','structure','legacy'])
        for r in (a,alias,b,c,far):s.add(r,a)
        self.assertEqual(s.beam,[a,b,c])
        new=self.record(plan(3),80);s.add(new,new)
        self.assertEqual(s.beam,[new])

    def test_greedy_retains_only_best(self):
        a=self.record(plan(),100);b=self.record(plan(1),101)
        s=SearchBudget(1,['region','data']);s.add(a,a);s.add(b,a)
        self.assertEqual(s.beam,[a])

    def test_cost_priority_after_probes_and_structural_reserve(self):
        a=self.record(plan(),100);s=SearchBudget(1,['region','data']);s.add(a,a)
        self.assertEqual(s.choose(a),('structure',None))
        s.observe('structure',None,100,100,.1)
        self.assertEqual(s.choose(a)[0],'legacy');s.observe('legacy',a,100,100,.1)
        self.assertEqual(s.choose(a)[0],'region');s.observe('region',a,100,100,10.)
        self.assertEqual(s.choose(a),('structure',None));s.observe('structure',None,100,100,.1)
        self.assertEqual(s.choose(a)[0],'data');s.observe('data',a,100,100,10.)
        self.assertEqual(s.choose(a)[0],'legacy')
        s.exhaust('legacy',a);self.assertNotEqual(s.choose(a)[0],'legacy')

    def test_near_best_parent_can_cross_a_local_barrier_within_total_budget(self):
        a=self.record(plan(),100);b=self.record(plan(1),102)
        prefix=dict(calls=[dict(name='a',record=a),dict(name='b',record=b)],logical_calls=2,
                    elapsed_seconds=.1,best_record=a)
        generated=[]
        def generate(ir,value,raw,p,n,family,index,limit):
            generated.append((raw['makespan'],family))
            return [dict(name='child',plan=plan(0 if raw['makespan']==100 else 1,10),metadata={})],{}
        def evaluate(g,value,*args,**kwargs):
            return self.record(value,104 if value==plan(0,10) else 80)
        with patch.object(cp,'prefix_run',return_value=prefix),patch.object(cp.GraphIR,'from_path',return_value=graph(3,[])), \
             patch.object(cp,'local_order',return_value=(['region','data','structure','legacy'],{})), \
             patch.object(cp,'generate_component_candidates',return_value=([],{})), \
             patch.object(cp,'generate_operation_candidates',return_value=([],{})), \
             patch.object(cp,'local_candidates',side_effect=generate),patch.object(cp,'evaluate',side_effect=evaluate), \
             patch('common_run.known',side_effect=AssertionError('history forbidden')):
            s=cp.run('case_001',2,2,'budget_beam',self.root/'run',4,60,5)
        self.assertEqual(s['logical_calls'],4);self.assertEqual(s['new_calls'],4)
        self.assertEqual(generated,[(100,'legacy'),(102,'region')])
        self.assertEqual(s['calls'][-1]['parent_record'],b['record_path'])
        self.assertEqual(s['best_record']['metrics']['makespan'],80)

    def test_online_reward_uses_global_improvement_only(self):
        a=self.record(plan(),100);s=SearchBudget(1,['data']);s.add(a,a)
        s.observe('data',a,100,100,.2)
        self.assertEqual(s.stats['data']['gain'],0)
        s.observe('data',a,100,90,.2)
        self.assertAlmostEqual(s.stats['data']['gain'],.1)


if __name__=='__main__':unittest.main()
