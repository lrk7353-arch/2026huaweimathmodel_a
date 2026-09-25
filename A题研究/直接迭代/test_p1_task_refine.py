"""Counterexamples and randomized DAG invariants for Task transformations."""
import copy,random,unittest
from common_run import GraphIR,validate_plan
from p1_task_refine import coalesce,split_large,reschedule

def graph(n,edges):
    return GraphIR.from_graph({'ops':[{'id':i,'op':'ADD','pipe':'PIPE_M' if i%2 else 'PIPE_V','cycles':10+i} for i in range(n)],
      'tensors':[{'id':1000+i,'size':16,'pos':'UB'} for i in range(len(edges))],
      'edges':[e for k,(a,b) in enumerate(edges) for e in ({'source':a,'target':1000+k},{'source':1000+k,'target':b})]})
def owners(plan):
    core={s:c for c,seq in enumerate(plan['core_schedules']) for s in seq}
    return {o:core[s] for o,s in plan['node_to_subgraph'].items()}

class Tests(unittest.TestCase):
    def test_remote_path_prevents_unsafe_merge(self):
        ir=graph(3,[(0,1),(1,2)]);p={'node_to_subgraph':{'0':0,'1':1,'2':2},'core_schedules':[[0,2],[1]]}
        self.assertEqual(coalesce(ir,p,100),p)
    def test_independent_same_core_tasks_can_merge(self):
        ir=graph(3,[]);p={'node_to_subgraph':{'0':0,'1':1,'2':2},'core_schedules':[[0,2],[1]]}
        q=coalesce(ir,p,100);self.assertEqual(q['node_to_subgraph']['0'],q['node_to_subgraph']['2']);self.assertEqual(owners(q),owners(p))
    def test_random_dags_and_input_immutability(self):
        rng=random.Random(8123)
        for sample in range(25):
            n=30;ir=graph(n,[(i,j) for i in range(n) for j in range(i+1,n) if rng.random()<.07])
            p={'node_to_subgraph':{str(i):i for i in range(n)},'core_schedules':[[] for _ in range(4)]}
            for i in range(n):p['core_schedules'][rng.randrange(4)].append(i)
            saved=copy.deepcopy(p);original=copy.deepcopy(ir.graph)
            raw={'step3_by_task':{str(i):{'local_makespan':i+10} for i in range(n)}}
            for cap in (2,5,100):
                q=coalesce(ir,p,cap,seed=sample);validate_plan(ir,q);self.assertEqual(owners(q),owners(p))
                split=split_large(ir,q,3);validate_plan(ir,split);self.assertEqual(owners(split),owners(p))
            r=reschedule(ir,p,raw,'local',sample);validate_plan(ir,r);self.assertEqual(r['node_to_subgraph'],p['node_to_subgraph'])
            self.assertEqual(saved,p);self.assertEqual(original,ir.graph)

if __name__=='__main__':unittest.main()
