import unittest

from common_run import GraphIR, validate_plan
from event_frontier import Calendar, sealed_tasks, insertion_schedule, operation_plan
from unified_structure import Structure


class FrontierContracts(unittest.TestCase):
    def graph(self):
        return GraphIR.from_graph(dict(
            ops=[dict(id=i,op='ADD',pipe='PIPE_V',cycles=3) for i in range(5)],
            tensors=[dict(id=10+i,pos='UB',size=4) for i in range(2)],
            edges=[dict(source=0,target=10),dict(source=10,target=2),
                   dict(source=2,target=11),dict(source=11,target=3)]))

    def test_inserts_before_and_between_reservations(self):
        c=Calendar();c.insert(10,5,1,0);c.insert(30,5,2,1)
        self.assertEqual(c.slot(0,3),(0,0))
        self.assertEqual(c.slot(12,4),(15,1))
        self.assertEqual(c.slot(12,16),(35,2))

    def test_independent_other_core_does_not_force_cut(self):
        ir=self.graph()
        plan,_=sealed_tasks(ir,[0,1,2,3,4],{0:0,1:1,2:0,3:0,4:0},2)
        self.assertEqual(plan['node_to_subgraph']['0'],plan['node_to_subgraph']['3'])
        validate_plan(ir,plan)

    def test_dependency_emission_seals_task_and_prevents_return_cycle(self):
        ir=self.graph()
        plan,_=sealed_tasks(ir,[0,1,2,3,4],{0:0,1:1,2:1,3:0,4:0},2)
        self.assertNotEqual(plan['node_to_subgraph']['0'],plan['node_to_subgraph']['3'])
        self.assertEqual(plan['node_to_subgraph']['3'],plan['node_to_subgraph']['4'])
        validate_plan(ir,plan)

    def test_fixed_ownership_survives_gap_reordering(self):
        ir=self.graph();fixed={0:0,1:1,2:1,3:0,4:0}
        owners,order,_=insertion_schedule(Structure(ir),2,fixed=fixed)
        self.assertEqual(owners,fixed)
        self.assertEqual(set(order),set(ir.compute_ids))
        validate_plan(ir,operation_plan(ir,order,owners,2))


if __name__=='__main__':unittest.main()
