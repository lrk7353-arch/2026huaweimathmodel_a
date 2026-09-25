import copy,unittest
from common_run import validate_plan
from advanced_solver.tests.test_cache_refine import fixture,fifo_mock
from advanced_solver.cache_refine import _core_map
from p3_read_order import generate


class ReadOrderTests(unittest.TestCase):
    def test_exact_control_mapping_and_fixed_ownership(self):
        for name in ('short_compute_simultaneous','short_compute_staggered','long_follower_compute_simultaneous'):
            ir,plan,raw=fixture(name);before=copy.deepcopy((ir.graph,plan,raw))
            candidates,diag=generate(ir,plan,raw,2,12)
            self.assertEqual(before,(ir.graph,plan,raw));self.assertTrue(diag['result_validation']=='passed')
            self.assertTrue(candidates[0]['metadata']['is_reencoding_control'])
            for c in candidates:
                self.assertTrue(validate_plan(ir,c['plan']))
                self.assertEqual(c['plan']['node_to_subgraph'],candidates[0]['plan']['node_to_subgraph'])
                self.assertEqual(_core_map(c['plan']),_core_map(plan))
    def test_useful_work_can_precede_follower_without_fake_operations(self):
        ir,plan,raw=fixture('short_compute_simultaneous')
        candidates,_=generate(ir,plan,raw,2)
        moved=next(c for c in candidates if c['metadata']['mechanism']=='independent_work_before_follower')
        mapping=moved['plan']['node_to_subgraph'];order=moved['plan']['core_schedules'][1]
        self.assertLess(order.index(mapping['202']),order.index(mapping['201']))
    def test_limit_determinism_and_fifo_case(self):
        ir,plan,raw=fifo_mock()
        for limit in (1,3,12):
            a=generate(ir,plan,raw,1,limit);self.assertEqual(a,generate(ir,plan,raw,1,limit))
            self.assertLessEqual(len(a[0]),limit)
            for c in a[0]:self.assertTrue(validate_plan(ir,c['plan']))


if __name__=='__main__':unittest.main()
