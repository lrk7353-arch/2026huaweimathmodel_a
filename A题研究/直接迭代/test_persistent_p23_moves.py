import copy
import itertools
import unittest
from unittest.mock import patch

from common_run import GraphIR, validate_plan
from event_frontier import operation_plan
import persistent_p23_moves as moves


class PersistentP23Contracts(unittest.TestCase):
    def fixture(self, problem=2):
        ir = GraphIR.from_graph(dict(
            ops=[dict(id=i, op='ADD', pipe='PIPE_M' if i in (0,2,3) else 'PIPE_V', cycles=cost)
                 for i,cost in enumerate((50,30,20,40,20,30))],
            tensors=[dict(id=t, pos='DDR' if t==20 else 'UB', size=64) for t in (10,11,12,20)],
            edges=[dict(source=a,target=b) for a,b in ((0,10),(10,1),(1,11),(11,2),
                                                     (3,12),(12,4),(20,0),(20,3))]))
        owners={0:0,1:1,2:1,3:1,4:0,5:1}
        order=[0,3,5,4,1,2]
        plan=operation_plan(ir,order,owners,2)
        starts={0:10,1:100,2:130,3:12,4:90,5:20}
        compute={o:(owners[o],o) for o in order}
        events={compute[o]:dict(start=starts[o],end=starts[o]+ir.ops[o]['cycles'],core_id=owners[o]) for o in order}
        copies={
            (1,1000):dict(op='COPY_IN',start=60,end=100,core_id=1,original_tensors=[10],wait_priority=80),
            (0,1001):dict(op='COPY_IN',start=0,end=10,core_id=0,original_tensors=[20],wait_priority=5,
                          cache_tensor_id=20,cache_hit=False),
            (1,1002):dict(op='COPY_IN',start=0,end=12,core_id=1,original_tensors=[20],wait_priority=20,
                          cache_tensor_id=20,cache_hit=False)}
        raw=dict(problem=problem,makespan=150,capacity_bytes={'L1':524288,'UB':131072},cache_events=[])
        obs=dict(assignment=owners,events=events,compute=compute,copies=copies,
                 tails={compute[o]:150-starts[o] for o in order},
                 slack={compute[o]:0 for o in order},summary={'makespan':150})
        return ir,plan,raw,obs,order

    def test_lazy_build_and_no_parent_mutation(self):
        ir,plan,raw,obs,_=self.fixture()
        before=copy.deepcopy((ir.graph,plan,raw,obs))
        repair=moves._repair_order
        with patch.object(moves,'_repair_order',wraps=repair) as mocked:
            stream=moves.iter_candidates(ir,plan,raw,2,observation=obs)
            self.assertEqual(mocked.call_count,0)
            candidate=next(stream)
            self.assertEqual(mocked.call_count,1)
        validate_plan(ir,candidate['plan'])
        self.assertGreater(candidate['metadata']['moved_ops'],0)
        self.assertTrue(candidate['metadata']['outside_assignment_preserved'])
        self.assertEqual((ir.graph,plan,raw,obs),before)

    def test_suspension_does_not_exhaust_generation_budget(self):
        ir,plan,raw,obs,_=self.fixture()
        now=[100.]
        with patch.object(moves.time,'monotonic',side_effect=lambda:now[0]):
            stream=moves.iter_candidates(ir,plan,raw,2,observation=obs,seconds=1)
            first=next(stream)
            now[0]+=1000  # Models an external official evaluation during yield.
            second=next(stream)
        self.assertNotEqual(first['name'],second['name'])

    def test_generation_work_timeout_is_enforced(self):
        ir,plan,raw,obs,_=self.fixture()
        clock=itertools.count(step=.1)
        with patch.object(moves.time,'monotonic',side_effect=lambda:next(clock)):
            with self.assertRaises(TimeoutError):
                next(moves.iter_candidates(ir,plan,raw,2,observation=obs,seconds=.01))

    def test_regional_repair_keeps_outside_observed_order(self):
        ir,plan,raw,obs,order=self.fixture()
        region={1,2}
        repaired,_=moves._repair_order(ir,obs['assignment'],order,region,obs,
            raw['capacity_bytes'],moves._tensor_views(ir),lambda:None)
        self.assertEqual([o for o in repaired if o not in region],[o for o in order if o not in region])
        validate_plan(ir,operation_plan(ir,repaired,obs['assignment'],2))

    def test_p3_interleaves_fifo_joint_action_without_fake_work(self):
        ir,plan,raw,obs,_=self.fixture(3)
        stream=moves.iter_candidates(ir,plan,raw,2,observation=obs)
        first,second=next(stream),next(stream)
        self.assertEqual(second['metadata']['bottleneck']['kind'],'fifo_read_window')
        self.assertIn('first_reader',second['metadata']['action'])
        self.assertEqual(set(map(int,second['plan']['node_to_subgraph'])),set(ir.compute_ids))
        self.assertTrue(second['metadata']['bottleneck']['concurrent_cold_miss'])
        validate_plan(ir,second['plan'])

    def test_rejects_wrong_observation_before_proposal(self):
        ir,plan,raw,obs,_=self.fixture()
        obs['summary']['makespan']+=1
        with self.assertRaises(ValueError):
            next(moves.iter_candidates(ir,plan,raw,2,observation=obs))


if __name__=='__main__':
    unittest.main()
