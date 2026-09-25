"""Structural counterexamples and controller behavior for regional refinement."""
import copy
import gzip
import json
import random
import tempfile
import unittest
import contextlib
import io
from pathlib import Path
from unittest.mock import patch

from common_run import GraphIR, atomic_json, validate_plan
from test_p1_task_refine import graph, owners
from p1_joint_regions import generate, capped_regions
from p23_region_refine import grow, affinity
from advanced_solver.trace_refine import _tensor_views
import refine_regions
import p3_joint_reads


class StructuralTests(unittest.TestCase):
    def test_p1_split_and_reassign_exposes_parallel_branches(self):
        ir=graph(20,[(0,i) for i in range(1,19)]+[(i,19) for i in range(1,19)])
        plan={'node_to_subgraph':{str(i):0 for i in range(20)},'core_schedules':[[0],[],[]]}
        raw={'makespan':10000,'per_core_timeline':[{'core_id':c,'tasks':[{'task_id':0,'duration':10000}] if c==0 else []} for c in range(3)]}
        saved=copy.deepcopy((ir.graph,plan,raw))
        cs,_=generate(ir,plan,raw,3,limit=64)
        self.assertTrue(any(len(set(owners(c['plan']).values()))>1 for c in cs))
        for c in cs:validate_plan(ir,c['plan'])
        self.assertEqual(saved,(ir.graph,plan,raw))

    def test_p1_random_quotient_safety_and_coverage(self):
        rng=random.Random(420)
        for _ in range(8):
            n=20;ir=graph(n,[(a,b) for a in range(n) for b in range(a+1,n) if rng.random()<.1])
            plan={'node_to_subgraph':{str(i):0 for i in range(n)},'core_schedules':[[0],[]]}
            raw={'makespan':9000,'per_core_timeline':[{'core_id':0,'tasks':[{'task_id':0,'duration':9000}]},{'core_id':1,'tasks':[]}]}
            cs,_=generate(ir,plan,raw,2,limit=24)
            for c in cs:
                validate_plan(ir,c['plan'])
                self.assertEqual(set(map(int,c['plan']['node_to_subgraph'])),set(ir.compute_ids))

    def test_region_grows_beyond_three_without_crossing_owner(self):
        ir=graph(15,[(i,i+1) for i in range(14)])
        assignment={i:int(i>=10) for i in range(15)}
        region=grow(ir,0,assignment,affinity(ir,_tensor_views(ir)),8)
        self.assertEqual(len(region),8)
        self.assertTrue(all(assignment[o]==0 for o in region))
        limited=grow(ir,0,assignment,affinity(ir,_tensor_views(ir)),128,work_cap=30)
        for pipe in ('PIPE_M','PIPE_V'):
            self.assertLessEqual(sum(ir.ops[o]['cycles'] for o in limited if ir.ops[o]['pipe']==pipe),30)

    def test_mismatched_p1_schedule_is_rejected(self):
        ir=graph(4,[]);p={'node_to_subgraph':{str(i):0 for i in range(4)},'core_schedules':[[0],[]]}
        raw={'makespan':10,'per_core_timeline':[{'core_id':1,'tasks':[{'task_id':0,'duration':10}]},{'core_id':0,'tasks':[]}]}
        with self.assertRaises(ValueError):generate(ir,p,raw,2)


class ControllerTests(unittest.TestCase):
    def fixture(self,d,ident,makespan,plan,status='success',cache=False):
        p=d/f'p{ident}.json';q=d/f'r{ident}.json.gz';atomic_json(p,plan)
        with gzip.open(q,'wt') as f:json.dump({'makespan':makespan},f)
        return dict(status=status,problem=2,graph_path='case_001.json',plan_path=str(p),result_path=str(q),
                    record_path=str(d/f'rec{ident}.json'),cache_hit=cache,
                    metrics=dict(makespan=makespan,num_cores=2,data_movement_bytes=dict(added_copy_bytes=0)))

    def test_refresh_failure_charging_and_exact_budget(self):
        ir=graph(3,[])
        def plan(i):return {'node_to_subgraph':{str(o):o+i*3 for o in range(3)},'core_schedules':[[i*3,i*3+1],[i*3+2]]}
        with tempfile.TemporaryDirectory() as temp:
            d=Path(temp);old=self.fixture(d,0,100,plan(0));made=[];evaluated=[]
            def generation(ir,p,raw,problem,cores,policy,round_index,limit):
                made.append(raw['makespan'])
                return [dict(name=str(i),plan=plan(i),metadata={}) for i in range(1,12)],{}
            def evaluate(g,p,problem,ev,**kw):
                i=len(evaluated)+1;evaluated.append(i)
                return self.fixture(d,i,90 if i==1 else 95,p,'timeout' if i==2 else 'success',cache=(i==1))
            with patch.object(refine_regions.GraphIR,'from_path',return_value=ir),patch.object(refine_regions,'candidates',side_effect=generation),patch.object(refine_regions,'evaluate',side_effect=evaluate):
                s=refine_regions.run('case_001',old,d/'out',budget=5,seconds=60,cores=2)
            self.assertEqual(s['logical_calls'],5)
            self.assertEqual(s['new_calls'],4)
            self.assertEqual(s['failed_calls'],1)
            self.assertEqual(s['after'],90)
            self.assertIn(90,made[1:])
            self.assertEqual(s['stop_reason'],'call_budget')

    def test_no_improvement_still_consumes_pending_after_expansions(self):
        ir=graph(3,[])
        def plan(i):return {'node_to_subgraph':{str(o):o+i*3 for o in range(3)},'core_schedules':[[i*3,i*3+1],[i*3+2]]}
        with tempfile.TemporaryDirectory() as temp:
            d=Path(temp);old=self.fixture(d,0,100,plan(0));count=0
            def generation(*args,**kwargs):return [dict(name=str(i),plan=plan(i),metadata={}) for i in range(1,25)],{}
            def evaluate(g,p,problem,ev,**kw):
                nonlocal count
                count+=1;return self.fixture(d,count,110,p)
            with patch.object(refine_regions.GraphIR,'from_path',return_value=ir),patch.object(refine_regions,'candidates',side_effect=generation),patch.object(refine_regions,'evaluate',side_effect=evaluate):
                s=refine_regions.run('case_001',old,d/'out',budget=24,seconds=60,cores=2)
            self.assertEqual(s['logical_calls'],24)
            self.assertEqual(s['after'],100)

    def test_every_improvement_can_refresh_until_budget(self):
        ir=graph(3,[])
        def plan(i):return {'node_to_subgraph':{str(o):o+i*3 for o in range(3)},'core_schedules':[[i*3,i*3+1],[i*3+2]]}
        with tempfile.TemporaryDirectory() as temp:
            d=Path(temp);old=self.fixture(d,0,100,plan(0));count=0;observed=[]
            def generation(ir,p,raw,*args,**kw):
                observed.append(raw['makespan']);i=100-raw['makespan']+1
                return [dict(name=str(i),plan=plan(i),metadata={})],{}
            def evaluate(g,p,problem,ev,**kw):
                nonlocal count
                count+=1;return self.fixture(d,count,100-count,p)
            with patch.object(refine_regions.GraphIR,'from_path',return_value=ir),patch.object(refine_regions,'candidates',side_effect=generation),patch.object(refine_regions,'evaluate',side_effect=evaluate):
                s=refine_regions.run('case_001',old,d/'out',budget=12,seconds=60,cores=2)
            self.assertEqual(s['after'],88)
            self.assertEqual(observed,list(range(100,88,-1)))


class ReadTests(unittest.TestCase):
    def test_joint_reads_keep_assignment_and_distinct_bundle_tensors(self):
        ir=graph(12,[(0,2),(1,3),(2,4),(3,5)])
        order=list(range(12));rank={o:o for o in order};assignment={o:o%2 for o in order}
        plan={'node_to_subgraph':{str(o):o for o in order},'core_schedules':[order[::2],order[1::2]]}
        groups={100:[dict(core_id=0,end=0,time=0),dict(core_id=1,end=1,time=0)],
                101:[dict(core_id=0,end=0,time=0),dict(core_id=1,end=1,time=0)]}
        view=dict(core_by_op=assignment,groups=groups,consumers={100:{2,5},101:{4,7}},
                  compute={o:dict(start=o+2) for o in order})
        targets=[dict(tensor_id=t,core_id=c,target_consumer=o,category='concurrent_cold_miss',priority_score=5)
                 for t,c,o in [(100,0,2),(100,1,5),(100,1,5),(101,1,7)]]
        with patch.object(p3_joint_reads,'_validate_observation',return_value=view), \
             patch.object(p3_joint_reads,'_observed_order_and_tails',return_value=(order,rank,{})), \
             patch.object(p3_joint_reads,'generate_cache_candidates',return_value=([],{'ranked_targets':targets})):
            cs,diag=p3_joint_reads.generate(ir,plan,{},2,limit=100)
        self.assertEqual(diag['target_count'],3)
        self.assertTrue(cs)
        for c in cs:
            validate_plan(ir,c['plan']);self.assertEqual(owners(c['plan']),owners(plan))
            ts=c['metadata']['targets']
            self.assertEqual(len(ts),len(set(ts)))


class EntryTests(unittest.TestCase):
    def test_p3_retains_strong_old_neighborhood_by_default(self):
        for problem,expected in ((1,'joint'),(2,'joint'),(3,'legacy')):
            with tempfile.TemporaryDirectory() as temp:
                result=dict(case='case_001',problem=problem,before=100,after=90,logical_calls=1,new_calls=1,elapsed_seconds=.1,stop_reason='call_budget')
                with patch('sys.argv',['refine_regions.py','--case','1','--problem',str(problem),'--out',str(Path(temp)/'out')]), \
                     patch.object(refine_regions,'known',return_value={('case_001',problem,5):{}}), \
                     patch.object(refine_regions,'run',return_value=result) as call,contextlib.redirect_stdout(io.StringIO()):
                    refine_regions.main()
                self.assertEqual(call.call_args.args[6],expected)


if __name__=='__main__':unittest.main()
