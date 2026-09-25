import hashlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parent))
from common_run import atomic_json
from critical_wait_candidates import signature
from test_p1_task_refine import graph
import wait_search


class BudgetContract(unittest.TestCase):
    def test_start_and_failed_candidate_are_both_charged_duplicates_are_not(self):
        ir=graph(2,[]);plan=dict(node_to_subgraph={'0':0,'1':1},core_schedules=[[0],[1]])
        changed=dict(node_to_subgraph={'0':0,'1':1},core_schedules=[[1],[0]])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);pp=root/'initial.json';atomic_json(pp,plan)
            rec=dict(status='success',problem=2,cache_hit=False,plan_path=str(pp),result_path='unused',record_path='parent',
                hashes=dict(plan_sha256=hashlib.sha256(signature(plan).encode()).hexdigest()),
                metrics=dict(makespan=100,num_cores=2,data_movement_bytes=dict(added_copy_bytes=20,spill_added_copy_bytes=0),memory_peak_by_core={'0':{'UB':0}}))
            def fake_generation(cmd,**kwargs):
                atomic_json(cmd[-1],dict(status='success',candidates=[dict(name='duplicate',plan=plan),dict(name='failure',plan=changed)],diagnostics={}))
                return SimpleNamespace(returncode=0,stderr='')
            with patch.object(wait_search.GraphIR,'from_path',return_value=ir),patch.object(wait_search,'evaluate',
                side_effect=[rec,dict(status='timeout',cache_hit=False)]) as evaluator,patch.object(wait_search.subprocess,'run',side_effect=fake_generation):
                s=wait_search.run('case_001',2,2,plan,root/'run','wait_joint',budget=2)
            self.assertEqual(evaluator.call_count,2);self.assertEqual(s['logical_calls'],2)
            self.assertEqual(s['failures'],1);self.assertEqual(s['best_record'],rec)

    def test_nonpositive_budget_rejected_before_output_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'no_output'
            with self.assertRaises(ValueError):wait_search.run('case_001',2,2,{},out,'wait_joint',budget=0)
            self.assertFalse(out.exists())

    def test_wrong_scenario_or_core_count_rejected_before_any_evaluation(self):
        plan=dict(node_to_subgraph={'0':0},core_schedules=[[0],[]])
        for problem,cores in ((0,2),(True,2),(2,0),(2,6),(2,3)):
            with self.subTest(problem=problem,cores=cores),tempfile.TemporaryDirectory() as tmp:
                out=Path(tmp)/'no_output'
                with patch.object(wait_search,'evaluate') as evaluator:
                    with self.assertRaises(ValueError):wait_search.run('case_001',problem,cores,plan,out,'wait_joint')
                evaluator.assert_not_called();self.assertFalse(out.exists())


if __name__=='__main__':unittest.main()
