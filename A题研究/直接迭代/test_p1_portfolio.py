"""Budget and fallback invariants without invoking the expensive simulator."""
import gzip,tempfile,unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import p1_portfolio as module
from common_run import atomic_json


def candidate(i,single=False,proxy=1):
    mapping={'0':i*2} if single else {'0':i*2,'1':i*2+1}
    return dict(name=f'candidate{i}',plan={'node_to_subgraph':mapping,'core_schedules':[list(mapping.values()),[],[],[],[]]},
                metadata={'lower_bound':0,'proxy':proxy})


class Tests(unittest.TestCase):
    def exercise(self,budget,single=False):
        with tempfile.TemporaryDirectory() as temporary,ExitStack() as stack:
            root=Path(temporary);observed=[]
            def evaluate(graph,plan,*args,**kwargs):
                observed.append(plan);i=len(observed);p=root/f'plan{i}.json';atomic_json(p,plan)
                raw=root/f'raw{i}.json.gz'
                with gzip.open(raw,'wt') as f:f.write('{}')
                return dict(status='success',cache_hit=True,metrics={'makespan':1000-i,'data_movement_bytes':{'added_copy_bytes':0}},
                            plan_path=str(p),result_path=str(raw))
            stack.enter_context(patch.object(module.GraphIR,'from_path',return_value=SimpleNamespace(compute_ids=[0,1])))
            stack.enter_context(patch.object(module,'generate_component_candidates',return_value=([candidate(i,single) for i in range(4)],{})))
            stack.enter_context(patch.object(module,'generate_selective_candidates',return_value=([candidate(i,single) for i in range(4,24)],{'heavy_component_ids':[0]})))
            stack.enter_context(patch.object(module,'evaluate',side_effect=evaluate))
            stack.enter_context(patch.object(module,'validate_plan'))
            stack.enter_context(patch.object(module,'task_lower_bound',return_value={'value':0}))
            stack.enter_context(patch.object(module,'boundary_ddr_lower_bound',return_value={'lower_bound':0}))
            stack.enter_context(patch.object(module,'known',side_effect=AssertionError('historical lookup forbidden')))
            stack.enter_context(patch('p1_tensor_regions.generate',return_value=[candidate(30,single,10**9 if single else 1)]))
            local=stack.enter_context(patch('p1_task_refine.generate',return_value=[candidate(i,single) for i in range(40,50)]))
            result=module.run('case_001',5,root/'out',budget,60)
            self.assertEqual(len(observed),budget)
            self.assertEqual(result['logical_calls'],budget)
            self.assertEqual(result['new_calls'],0)  # Cache hits still consume logical calls.
            self.assertEqual(result['best_record']['metrics']['makespan'],1000-budget)
            if single:self.assertEqual(local.call_count,0)
            return result
    def test_budget_caps_including_cache_hits(self):
        for budget in (1,2,3,6,12):
            with self.subTest(budget=budget):self.exercise(budget)
    def test_single_task_returns_allowance_to_partition(self):
        s=self.exercise(12,True)
        self.assertEqual(sum(t['phase']=='partition_fallback' for t in s['evaluations']),4)
    def test_diversity_preserves_candidates_and_within_group_order(self):
        rows=[dict(name=f'{kind}_{scale}_{i}',metadata=dict(partition=kind,scale=scale))
              for kind,scale,count in [('whole',1,1),('phase',1,4),('phase',2,3),('quantile',1,2)] for i in range(count)]
        ordered=module.diversify(rows)
        self.assertEqual(sorted(r['name'] for r in ordered),sorted(r['name'] for r in rows))
        self.assertEqual([r['name'] for r in ordered[:4]],['whole_1_0','phase_1_0','phase_2_0','quantile_1_0'])
        for kind,scale in [('whole',1),('phase',1),('phase',2),('quantile',1)]:
            self.assertEqual([r for r in rows if (r['metadata']['partition'],r['metadata']['scale'])==(kind,scale)],
                             [r for r in ordered if (r['metadata']['partition'],r['metadata']['scale'])==(kind,scale)])

if __name__=='__main__':unittest.main()
