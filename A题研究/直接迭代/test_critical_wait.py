"""Synthetic official-contract checks; these are not development-panel scores."""
import copy
import sys
import unittest
from common_run import DATA,GraphIR,validate_plan
from test_p1_task_refine import graph
from advanced_solver.trace_refine import _runs_plan
from critical_wait_candidates import generate,pipeline_order
from wait_observation import observe,pool_intervals

sys.path.insert(0,str(DATA.parent/'code'))
from multicore_cut_evaluate_problem_1 import evaluate_scene_a
from multicore_cut_evaluate_problem_2 import evaluate_scene_b
from multicore_cut_evaluate_problem_3 import evaluate_problem_3


class WaitTests(unittest.TestCase):
    def fixture(self,p):
        g=graph(8,[(0,2),(1,3),(2,4),(3,5),(4,6),(5,7)]).graph
        g['tensors'].append(dict(id=9000,pos='L1',size=4096))
        g['edges'] += [dict(source=9000,target=o) for o in (0,1,4,5)]
        ir=GraphIR.from_graph(g);plan=_runs_plan(ir,list(ir.compute_ids),{o:o%2 for o in ir.compute_ids},2,2)
        if p==1:raw=evaluate_scene_a(g,plan,60,{'L1':524288,'UB':131072},1000,100)
        elif p==2:raw=evaluate_scene_b(g,plan,60,{'L1':524288,'UB':131072},500)
        else:raw=evaluate_problem_3(g,plan,60,{'L1':524288,'UB':131072},500,1048576,250)
        return ir,plan,raw

    def test_pool_time_is_union_not_sum_of_waits(self):
        d,c=pool_intervals({1:dict(start=0,end=10),2:dict(start=5,end=15)})
        self.assertEqual(d,dict(busy_cycles=15,overlap_cycles=5,peak_concurrent=2))
        self.assertEqual(c,{1:2.5,2:2.5})

    def test_official_copy_paths_dependencies_and_candidates_all_scenarios(self):
        for p in (1,2,3):
            ir,plan,raw=self.fixture(p);saved=copy.deepcopy((ir.graph,plan,raw))
            obs=observe(ir,plan,raw,p);s=obs['summary']
            self.assertEqual(sum(s['path_bytes'].values()),raw['data_movement_bytes']['scheduled_copy_bytes'])
            if p==3:self.assertEqual(s['path_bytes'].get('CACHE_READ',0),raw['cache_stats']['hit_bytes'])
            cs,diag=generate(ir,plan,raw,2,24)
            self.assertTrue(cs)
            for c in cs:validate_plan(ir,c['plan'])
            self.assertEqual(saved,(ir.graph,plan,raw))
            order,details=pipeline_order(ir,obs['assignment'],list(ir.compute_ids),obs,raw['capacity_bytes'])
            validate_plan(ir,_runs_plan(ir,order,obs['assignment'],2,1))

    def test_corrupt_copy_accounting_rejected(self):
        ir,plan,raw=self.fixture(3);raw['data_movement_bytes']['scheduled_copy_bytes']+=1
        with self.assertRaisesRegex(AssertionError,'COPY accounting'):observe(ir,plan,raw,3)


if __name__=='__main__':unittest.main()
