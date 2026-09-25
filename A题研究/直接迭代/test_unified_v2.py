"""Counterexamples for region legality, scene semantics and budget accounting."""
import copy
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from common_run import GraphIR, validate_plan
from test_p1_task_refine import graph
from unified_structure import Structure, candidate_stream, owner_map
from unified_solver import solve_unified


class StructureTests(unittest.TestCase):
    def test_shared_logical_tensor_is_one_hyperedge(self):
        g = graph(3, []).graph
        g['tensors'] = [{'id': 1000, 'size': 4096, 'pos': 'UB'}]
        g['edges'] = [{'source': 0, 'target': 1000}, {'source': 1000, 'target': 1}, {'source': 1000, 'target': 2}]
        s = Structure(GraphIR.from_graph(g))
        self.assertEqual(len(s.tensors), 1)
        self.assertEqual(s.tensors[1000].consumers, (1, 2))
        # Both consumers on the same remote core: exactly one COPY pair.
        _, meta = s.assign([[0], [1], [2]], 2, 2, fixed={0: 0, 1: 1, 2: 1})
        self.assertEqual(meta['proxy_copy_bytes'], 8192)

    def test_logical_input_keys_are_not_collapsed_to_root_ddr(self):
        g = graph(2, []).graph
        g['ops'].append({'id': 9, 'op': 'COPY_IN', 'pipe': 'PIPE_MTE2', 'cycles': 10})
        g['tensors'] = [{'id': 1000, 'size': 64, 'pos': 'DDR'}, {'id': 1001, 'size': 64, 'pos': 'UB'}]
        g['edges'] = [{'source': 1000, 'target': 9}, {'source': 9, 'target': 1001},
                      {'source': 1001, 'target': 0}, {'source': 1001, 'target': 1}]
        s = Structure(GraphIR.from_graph(g))
        self.assertEqual(set(s.tensors), {1001})
        self.assertFalse(s.tensors[1001].producers)

    def test_core_return_is_preserved_as_multiple_acyclic_regions(self):
        ir = graph(3, [(0, 1), (1, 2)])
        plan, _ = Structure(ir).assign([[0], [1], [2]], 2, 2, fixed={0: 0, 1: 1, 2: 0})
        self.assertEqual(plan['core_schedules'], [[0, 2], [1]])
        validate_plan(ir, plan)

    def test_micro_forks_can_fuse_without_losing_large_branch_options(self):
        ir = graph(5, [(0, 1), (0, 2), (1, 3), (2, 3), (3, 4)])
        s = Structure(ir)
        self.assertEqual(len(s.regions(2, family='branch')), 1)
        for o in ir.graph['ops']:
            o['cycles'] *= 1000
        ir = GraphIR.from_graph(ir.graph)
        self.assertGreater(len(Structure(ir).regions(2, family='branch')), 1)

    def test_random_dags_all_scenes_all_resolutions_immutable_and_name_independent(self):
        rng = random.Random(761)
        for _ in range(10):
            ir = graph(30, [(a, b) for a in range(30) for b in range(a + 1, 30) if rng.random() < .08])
            original = copy.deepcopy(ir.graph)
            s = Structure(ir)
            for scene in (1, 2, 3):
                pool = list(candidate_stream(s, scene, 3))
                self.assertTrue(pool)
                for c in pool:
                    validate_plan(ir, c['plan'])
                    self.assertEqual(set(owner_map(c['plan'])), set(ir.compute_ids))
            ir.path = Path('/unseen-name.json')
            self.assertEqual(list(candidate_stream(s, 1, 3)), list(candidate_stream(Structure(ir), 1, 3)))
            self.assertEqual(ir.graph, original)

    def test_cycle_after_contraction_is_rejected(self):
        ir = graph(3, [(0, 1), (1, 2)])
        with self.assertRaises(ValueError):
            Structure(ir).assign([[0, 2], [1]], 1, 2)

    def test_zero_construction_budget_stops(self):
        with self.assertRaises(TimeoutError):
            Structure(graph(2, [])).regions(2, deadline=0)


class BudgetTests(unittest.TestCase):
    def test_warm_evaluation_charged_and_new_candidate_gets_second_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ir = graph(3, [])
            path = root / 'input.json'
            path.write_text(json.dumps(ir.graph))
            warm = {'node_to_subgraph': {'0': 0, '1': 0, '2': 0}, 'core_schedules': [[0], []]}
            old = {'name': 'old', 'plan': {'node_to_subgraph': {'0': 0, '1': 1, '2': 1}, 'core_schedules': [[0], [1]]}, 'metadata': {}}
            new = {'name': 'new', 'plan': {'node_to_subgraph': {'0': 0, '1': 1, '2': 2}, 'core_schedules': [[0, 2], [1]]}, 'metadata': {}}
            statuses = iter([('success', 100), ('execution_cycle', 0), ('success', 90)])
            def evaluation(*args, **kwargs):
                status, t = next(statuses)
                return dict(status=status, metrics={'makespan': t, 'data_movement_bytes': {'added_copy_bytes': 0}},
                            elapsed_seconds=.01, cache_hit=False)
            with patch('unified_solver.seeds', return_value=iter([old])), patch('unified_solver.candidate_stream', return_value=iter([new])), patch('unified_solver.evaluate', side_effect=evaluation):
                result = solve_unified(path, 3, 2, root / 'out', incumbent=warm, call_budget=3, stage='A')
            self.assertEqual([x['name'] for x in result['evaluations']], ['explicit_incumbent', 'new', 'old'])
            self.assertEqual(result['logical_calls'], 3)
            self.assertEqual(result['new_calls'], 3)
            self.assertEqual(result['best_record']['metrics']['makespan'], 90)
            self.assertEqual(result['mode'], 'warm_explicit_charged')


if __name__ == '__main__':
    unittest.main()
