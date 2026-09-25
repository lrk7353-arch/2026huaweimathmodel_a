import gzip
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from common_run import atomic_json
from test_p1_portfolio import candidate
import p1_portfolio_reserved as module


def c(i):
    x=candidate(i)
    x['metadata'].update(partition='test',scale=1)
    return x


class ReservedTests(unittest.TestCase):
    def exercise(self,budget,refresh):
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);observed=[]
            def evaluate(graph,plan,*args,**kwargs):
                observed.append(plan);i=len(observed)
                path=root/f'{i}.json';atomic_json(path,plan)
                raw=root/f'{i}.json.gz'
                with gzip.open(raw,'wt') as f:f.write('{}')
                return dict(status='success',cache_hit=True,
                    metrics={'makespan':1000-i,'data_movement_bytes':{'added_copy_bytes':0}},
                    plan_path=str(path),result_path=str(raw))
            stack.enter_context(patch.object(module.GraphIR,'from_path',return_value=SimpleNamespace(compute_ids=[0,1])))
            stack.enter_context(patch.object(module,'generate_component_candidates',return_value=([c(i) for i in range(4)],{})))
            stack.enter_context(patch.object(module,'generate_selective_candidates',return_value=([c(i) for i in range(4,24)],{'heavy_component_ids':[0]})))
            stack.enter_context(patch.object(module,'evaluate',side_effect=evaluate))
            stack.enter_context(patch.object(module,'validate_plan'))
            stack.enter_context(patch.object(module,'known',side_effect=AssertionError('no historical incumbents')))
            stack.enter_context(patch('p1_tensor_regions.generate',return_value=[c(30),c(31)]))
            stack.enter_context(patch('p1_task_refine.generate',return_value=[c(i) for i in range(40,55)]))
            s=module.run('case_001',5,root/'out',budget,60,refresh=refresh)
            self.assertEqual(len(observed),budget)
            self.assertEqual(s['logical_calls'],budget)
            self.assertEqual(s['new_calls'],0)
            self.assertEqual(s['prefix_calls'],budget-{8:3,12:4,16:6}[budget])
            self.assertEqual(sum(t['phase']=='task_refine' for t in s['evaluations']),{8:3,12:4,16:6}[budget])
            return s,observed

    def test_prefix_is_identical_and_task_allowance_is_protected(self):
        for budget in (8,12,16):
            with self.subTest(budget=budget):
                single,a=self.exercise(budget,False)
                iterative,b=self.exercise(budget,True)
                self.assertEqual(a[:single['prefix_calls']],b[:iterative['prefix_calls']])
                self.assertEqual(single['generations'],1)
                self.assertGreater(iterative['generations'],1)


if __name__=='__main__':unittest.main()
