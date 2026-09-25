"""Counterexamples for COPY accounting and live-tensor topological ordering."""
import copy
import random
import unittest
from unittest.mock import patch

from common_run import GraphIR, validate_plan
from test_p1_task_refine import graph
from advanced_solver.trace_refine import _runs_plan
from p23_data_refine import partition_copy_bytes, lifetime_order, reuse_proposals, generate


class DataTests(unittest.TestCase):
    def test_roots_cross_routes_and_final_output_are_counted_separately(self):
        g = graph(3, [(0,1), (0,2)]).graph
        g['tensors'] += [dict(id=2000,pos='UB',size=100), dict(id=2001,pos='UB',size=50)]
        g['edges'] += [dict(source=2000,target=i) for i in (0,2)] + [dict(source=2,target=2001)]
        ir = GraphIR.from_graph(g)
        # Root input: 2 * 100; remote tensor pair: 2 * 16; output: 50.
        self.assertEqual(partition_copy_bytes(ir,{0:0,1:0,2:1}),282)
        # Collocation removes BOTH read replication and the remote write/read.
        self.assertEqual(partition_copy_bytes(ir,{0:0,1:0,2:0}),150)

    def test_final_write_remains_when_output_also_has_compute_reader(self):
        g=graph(2,[(0,1)]).graph
        g['ops'].append(dict(id=20,op='COPY_OUT',pipe='PIPE_MTE3',cycles=1))
        g['edges'].append(dict(source=1000,target=20))
        ir=GraphIR.from_graph(g)
        self.assertEqual(partition_copy_bytes(ir,{0:0,1:1}),48)

    def test_lifetime_order_finishes_one_branch_before_opening_another(self):
        g=graph(4,[(0,1),(2,3)]).graph
        for tensor in g['tensors']: tensor['size']=100000
        ir=GraphIR.from_graph(g);assignment={o:0 for o in ir.compute_ids}
        order,peak=lifetime_order(ir,assignment,[0,2,1,3],window=8)
        self.assertEqual(order,[0,1,2,3]);self.assertEqual(peak['0:UB'],100000)

    def test_random_order_coverage_topology_and_input_immutability(self):
        rng=random.Random(1101)
        for _ in range(12):
            ir=graph(35,[(i,j) for i in range(35) for j in range(i+1,35) if rng.random()<.08])
            assignment={o:rng.randrange(3) for o in ir.compute_ids};saved=copy.deepcopy((ir.graph,assignment))
            for window in (1,8,64):
                order,peak=lifetime_order(ir,assignment,list(ir.compute_ids),window)
                validate_plan(ir,_runs_plan(ir,order,assignment,3,max_run_ops=1))
                self.assertTrue(all(v>=0 for v in peak.values()))
            self.assertEqual(saved,(ir.graph,assignment))

    def test_root_reuse_proposal_can_join_disconnected_coconsumers(self):
        g=graph(4,[(0,1),(2,3)]).graph
        g['tensors'].append(dict(id=2000,pos='L1',size=4096))
        g['edges'] += [dict(source=2000,target=i) for i in (0,2)]
        ir=GraphIR.from_graph(g);a={0:0,1:0,2:1,3:1}
        proposals=reuse_proposals(ir,a,{},2)
        whole=next(p for p in proposals if p['family']=='input_components' and len(p['changes'])==2)
        self.assertEqual(whole['partition_copy_delta'],-4096)

    def test_observation_copy_mismatch_rejected(self):
        ir=graph(2,[(0,1)]);p=_runs_plan(ir,[0,1],{0:0,1:1},2,1)
        raw={'data_movement_bytes':{'scheduled_copy_bytes':999,'spill_added_copy_bytes':0}}
        with patch('p23_data_refine._trace',return_value=({},[],{})):
            with self.assertRaisesRegex(ValueError,'COPY model'):generate(ir,p,raw,2)


if __name__=='__main__':unittest.main()
