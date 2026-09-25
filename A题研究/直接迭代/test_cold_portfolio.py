"""Shared budget, provenance, refresh and exhaustion contracts for cold search."""
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import cold_portfolio as cp
from test_p1_task_refine import graph


def plan(i):
    return {'node_to_subgraph':{'0':i*3,'1':i*3+1,'2':i*3+2},'core_schedules':[[i*3,i*3+1],[i*3+2]]}


class ColdTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.counter=0;self.generated=[];self.attempted=[]
        self.ir=graph(3,[])

    def record(self,value,span=100,status='success',cache=False):
        self.counter+=1;out=self.root/str(self.counter);out.mkdir()
        cp.atomic_json(out/'plan.json',value)
        with gzip.open(out/'result.gz','wt') as f:json.dump({'makespan':span},f)
        return dict(status=status,cache_hit=cache,problem=2,graph_path='case_001.json',plan_path=str(out/'plan.json'),
            record_path=str(out/'record.json'),result_path=str(out/'result.gz'),
            metrics=dict(makespan=span,num_cores=2,data_movement_bytes=dict(added_copy_bytes=0)))

    def invoke(self,budget,prefix,generator,evaluate=None):
        def default_eval(g,value,p,out,**kw):
            self.attempted.append(value);return self.record(value,100-len(self.attempted))
        with patch.object(cp,'prefix_run',return_value=prefix),patch.object(cp.GraphIR,'from_path',return_value=self.ir), \
             patch.object(cp,'local_order',return_value=(['region'],{})),patch.object(cp,'local_candidates',side_effect=generator), \
             patch.object(cp,'evaluate',side_effect=evaluate or default_eval),patch('common_run.known',side_effect=AssertionError('history forbidden')):
            return cp.run('case_001',2,2,'integrated',self.root/'run',budget,60,5)

    def prefix(self,records,best=None):
        return dict(calls=[dict(name='prefix',record=r,accepted=r is best) for r in records],logical_calls=len(records),
                    elapsed_seconds=.1,best_record=best)

    def test_failures_cache_and_prefix_share_one_budget(self):
        good=self.record(plan(0),cache=True);bad=self.record(plan(1),status='timeout')
        def generate(ir,p,raw,*args):
            self.generated.append(raw['makespan'])
            return [dict(name=str(i),plan=plan(i),metadata={}) for i in range(8)],{}
        s=self.invoke(6,self.prefix([good,bad],good),generate)
        self.assertEqual(s['logical_calls'],6);self.assertEqual(s['new_calls'],5)
        self.assertEqual(len(self.attempted),4)
        self.assertFalse(any(cp.exact(v)==cp.exact(plan(1)) for v in self.attempted))
        self.assertGreater(len(set(self.generated)),1)

    def test_all_duplicate_first_scale_still_tries_second_scale(self):
        old=self.record(plan(0))
        def generate(ir,p,raw,problem,cores,family,index,limit):
            return [dict(name=str(index),plan=plan(index),metadata={})],{}
        s=self.invoke(2,self.prefix([old],old),generate)
        self.assertEqual(s['logical_calls'],2);self.assertEqual(self.attempted,[plan(1)])

    def test_wrapper_exception_is_charged(self):
        old=self.record(plan(0))
        def generate(*args):return [dict(name=str(i),plan=plan(i),metadata={}) for i in (1,2,3)],{}
        def fail(*args,**kwargs):raise RuntimeError('synthetic failure')
        s=self.invoke(4,self.prefix([old],old),generate,fail)
        self.assertEqual(s['logical_calls'],4)
        self.assertTrue(all(c['record']['status']=='wrapper_exception' for c in s['calls'][1:]))
        self.assertEqual(s['best_record'],old)

    def test_prefix_cannot_inject_an_uncharged_incumbent(self):
        a=self.record(plan(0));b=self.record(plan(1),90)
        with self.assertRaisesRegex(ValueError,'incumbent mismatch'):
            self.invoke(4,self.prefix([a],b),lambda *args:([],{}))

    def test_generation_time_can_exhaust_budget_before_evaluation(self):
        old=self.record(plan(0));clock=[0.]
        def generate(*args):
            clock[0]=70.
            return [dict(name='late',plan=plan(1),metadata={})],{}
        with patch.object(cp.time,'monotonic',side_effect=lambda:clock[0]):
            s=self.invoke(4,self.prefix([old],old),generate)
        self.assertEqual(s['logical_calls'],1);self.assertEqual(s['stop_reason'],'time_budget')

    def test_failed_prefix_can_recover_and_enable_local_search(self):
        bad=self.record(plan(0),status='timeout')
        def generate(*args):return [dict(name='local',plan=plan(2),metadata={})],{}
        with patch.object(cp,'generate_component_candidates',return_value=([dict(name='recovery',plan=plan(1),metadata={})],{})), \
             patch.object(cp,'generate_operation_candidates',return_value=([],{})):
            s=self.invoke(3,self.prefix([bad]),generate)
        self.assertEqual(s['logical_calls'],3);self.assertEqual(s['status'],'success')
        self.assertTrue(any(stage.get('after_recovery') for stage in s['stages']))

    def test_component_wcc_pair_consumes_remaining_budget_and_keeps_parent(self):
        old=self.record(plan(0));prefix=self.prefix([old],old);prefix['best_component']=old
        probe=dict(name='paired_wcc',plan=plan(2),metadata={})
        with patch.object(cp,'prefix_run',return_value=prefix),patch.object(cp.GraphIR,'from_path',return_value=self.ir), \
             patch.object(cp,'local_order',return_value=(['structure'],{})), \
             patch.object(cp,'generate_component_candidates',return_value=([dict(name='layout',plan=plan(1),metadata={})],{})), \
             patch.object(cp,'generate_operation_candidates',return_value=([],{})), \
             patch.object(cp,'generate_wcc_candidates',return_value=([probe],{})), \
             patch('p23_observed.select_wcc_probe',side_effect=[(None,None),(probe,{})]), \
             patch.object(cp,'evaluate',side_effect=lambda g,p,*a,**kw:self.record(p,90 if p==plan(1) else 80)):
            s=cp.run('case_001',2,2,'integrated',self.root/'pair',3,60,5)
        self.assertEqual(s['logical_calls'],3)
        self.assertEqual([c.get('phase') for c in s['calls'][1:]],['structure','structure_pair'])
        self.assertEqual(s['calls'][2]['parent_record'],s['calls'][1]['record']['record_path'])


if __name__=='__main__':unittest.main()
