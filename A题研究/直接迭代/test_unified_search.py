import copy
import time
import unittest
from test_p1_task_refine import graph
from common_run import validate_plan
from unified_structure import Structure, candidate_stream, owner_map
from unified_search import analytical_features, Ranker, ElitePool, joint_repairs


class SearchTests(unittest.TestCase):
    def test_fanout_routes_count_destinations_not_edges(self):
        g = graph(3, []).graph
        g['tensors'] = [{'id': 1000, 'size': 1024, 'pos': 'UB'}]
        g['edges'] = [{'source': 0, 'target': 1000}, {'source': 1000, 'target': 1}, {'source': 1000, 'target': 2}]
        from common_run import GraphIR
        s = Structure(GraphIR.from_graph(g))
        plan, _ = s.assign([[0], [1], [2]], 2, 2, fixed={0: 0, 1: 1, 2: 1})
        features = analytical_features(s, plan, 2)
        self.assertEqual(features['cross_routes'], 1)
        self.assertEqual(features['copy_bytes'], 2048)

    def test_ranker_uses_structural_measurements_and_penalizes_failures(self):
        s = Structure(graph(4, []))
        c = next(candidate_stream(s, 2, 2))
        f = analytical_features(s, c['plan'], 2)
        ranker = Ranker()
        ranker.observe('family', f, dict(status='success', metrics={'makespan': 2*f['proxy']}, elapsed_seconds=4))
        prediction, cost, success = ranker.predict('family', f)
        self.assertEqual(prediction, 2*f['proxy'])
        self.assertEqual(cost, 4)
        ranker.observe('family', f, dict(status='timeout', elapsed_seconds=60))
        self.assertLess(ranker.predict('family', f)[2], success)
        self.assertNotIn('case', str(ranker.rows))

    def test_region_repair_is_complete_and_preserves_outside_ownership(self):
        ir = graph(8, [(0, 2), (1, 3), (2, 4), (3, 5), (4, 6), (5, 7)])
        s = Structure(ir)
        a, _ = s.assign([[i] for i in s.topo], 2, 2)
        b, _ = s.assign([[i] for i in s.topo], 2, 2, fixed={i: i % 2 for i in range(8)})
        elite = ElitePool()
        for i, plan in enumerate((a, b)):
            c = dict(name=f'p{i}', plan=plan, metadata={}, features=analytical_features(s, plan, 2))
            elite.add(c, dict(status='success', metrics={'makespan': 100+i}))
        before = copy.deepcopy(elite.items)
        pool = list(joint_repairs(s, 2, 2, elite.items, time.monotonic()+5))
        self.assertTrue(pool)
        original_owner = owner_map(a)
        for c in pool:
            validate_plan(ir, c['plan'])
            new_owner = owner_map(c['plan'])
            changed_tasks = set(c['metadata']['destroyed_tasks'])
            for o in ir.compute_ids:
                if a['node_to_subgraph'][str(o)] not in changed_tasks:
                    self.assertEqual(original_owner[o], new_owner[o])
        self.assertEqual(elite.items, before)


if __name__ == '__main__':
    unittest.main()
