"""Graph-only mechanism tests; no official evaluations or performance claims."""
import copy
from pathlib import Path
import random
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import p1_convex_regions as module
from graph_ir import GraphIR


def ir_from_pairs(n, pairs, cycles=None):
    tensors, edges = [], []
    for k, (a, b) in enumerate(pairs):
        tid = n + 1 + k
        tensors.append({"id": tid, "pos": "UB", "size": 60})
        edges += [{"source": a, "target": tid}, {"source": tid, "target": b}]
    return GraphIR.from_graph({"ops": [{"id": i, "op": "ADD", "pipe": "PIPE_M" if i % 2 else "PIPE_V",
                                       "cycles": cycles[i - 1] if cycles is not None else 100}
                                      for i in range(1, n + 1)], "tensors": tensors, "edges": edges})


class ConvexRegionTests(unittest.TestCase):
    def test_leaving_and_reentering_group_merges_scc(self):
        ir = ir_from_pairs(3, [(1, 2), (2, 3)])
        blocks, diag = module._scc_coarsen(ir, [[1, 3], [2]], [1, 2, 3])
        self.assertEqual(blocks, [[1, 2, 3]])
        self.assertEqual(diag["groups_eliminated"], 1)
        self.assertEqual(diag["largest_scc_group_count"], 2)

    def test_individually_convex_crossed_branches_still_require_scc_merge(self):
        # r=1 -> a=2 -> b=3 -> t=6; r -> c=4 -> d=5 -> t.
        # X={a,d}, Y={b,c} are individually convex but X<->Y is a quotient cycle.
        ir = ir_from_pairs(6, [(1, 2), (2, 3), (3, 6), (1, 4), (4, 5), (5, 6)])
        order = module.topological_order(ir, "stable_id")
        blocks, diag = module._scc_coarsen(ir, [[1], [2, 5], [3, 4], [6]], order)
        self.assertEqual([set(b) for b in blocks], [{1}, {2, 3, 4, 5}, {6}])
        self.assertEqual(diag["nontrivial_scc_count"], 1)

    def test_random_partitions_scc_quotient_and_path_convexity(self):
        rng = random.Random(901)
        for _ in range(30):
            n = 18
            pairs = [(a, b) for a in range(1, n) for b in range(a + 1, n + 1) if rng.random() < .14]
            ir = ir_from_pairs(n, pairs)
            order = module.topological_order(ir, "stable_id")
            initial = [[] for _ in range(5)]
            for op in order: initial[rng.randrange(5)].append(op)
            blocks, diag = module._scc_coarsen(ir, [b for b in initial if b], order)
            mapping, outgoing = module._quotient(ir, blocks)
            self.assertTrue(all(a < b for a, targets in enumerate(outgoing) for b in targets))
            reachable = {op: set() for op in order}
            for op in reversed(order):
                for child in ir.successors[op]: reachable[op].add(child); reachable[op].update(reachable[child])
            for a in order:
                for middle in reachable[a]:
                    for end in reachable[middle]:
                        if mapping[a] == mapping[end]: self.assertEqual(mapping[middle], mapping[a])

    def test_monotone_bands_do_not_merge_same_core_label_across_bands(self):
        ir = ir_from_pairs(20, [(i, i + 1) for i in range(1, 20)], [0, 1, 100, 1000] * 5)
        order = module.topological_order(ir, "stable_id")
        affinity = {o: 0 for o in order}
        for metric in module.BAND_METRICS:
            groups, labels = module._band_groups(ir, order, affinity, {0}, 100, metric)
            blocks, diag = module._scc_coarsen(ir, groups, order)
            self.assertEqual(len(groups), len(blocks))
            self.assertGreater(len(blocks), 1)
            self.assertEqual(diag["groups_eliminated"], 0)

    def test_small_components_remain_intact(self):
        ir = ir_from_pairs(25, [(i, i + 1) for i in range(1, 20)] + [(21, 22), (23, 24)])
        order = module.topological_order(ir, "stable_id")
        affinity = {o: o % 3 for o in order}
        groups, _ = module._band_groups(ir, order, affinity, {0}, 10, "compute_start")
        blocks, _ = module._scc_coarsen(ir, groups, order)
        owner, _ = module._quotient(ir, blocks)
        for component in ir.components[1:]:
            self.assertEqual(len({owner[o] for o in component.nodes}), 1)

    def test_copy_contracted_dependencies_used_for_scc(self):
        raw = {"ops": [{"id": 1, "op": "ADD", "pipe": "PIPE_M", "cycles": 100},
                       {"id": 2, "op": "COPY_OUT", "pipe": "PIPE_MTE3", "cycles": 1},
                       {"id": 3, "op": "ADD", "pipe": "PIPE_V", "cycles": 100},
                       {"id": 4, "op": "ADD", "pipe": "PIPE_M", "cycles": 100}],
               "tensors": [{"id": t, "size": 60, "pos": "UB" if t != 12 else "DDR"} for t in (11, 12, 13)],
               "edges": [{"source": a, "target": b} for a, b in [(1, 11), (11, 2), (2, 12), (12, 3), (3, 13), (13, 4)]]}
        ir = GraphIR.from_graph(raw)
        blocks, _ = module._scc_coarsen(ir, [[1, 4], [3]], [1, 3, 4])
        self.assertEqual(blocks, [[1, 3, 4]])
        candidates, diag = module.generate_convex_candidates(ir, 2)
        self.assertEqual(candidates, [])
        self.assertTrue(diag["generation_failures"])

    def test_all_core_counts_determinism_coverage_and_input_immutability(self):
        pairs = [(i, i + 2) for i in range(1, 23)] + [(1, 2), (23, 24)]
        ir = ir_from_pairs(24, pairs)
        before = copy.deepcopy(ir.graph)
        for n in range(1, 6):
            first = module.generate_convex_candidates(ir, n, 12, 17)
            self.assertEqual(first, module.generate_convex_candidates(ir, n, 12, 17))
            candidates, diag = first
            self.assertTrue(candidates)
            self.assertLessEqual(len(candidates), 12)
            self.assertEqual(diag["official_calls"], 0)
            self.assertFalse(diag["pruning_performed"])
            self.assertEqual(len(candidates), len({module.object_digest(c["plan"]) for c in candidates}))
            for candidate in candidates:
                self.assertTrue(module.validate_plan(ir, candidate["plan"]))
                self.assertEqual(len(candidate["plan"]["core_schedules"]), n)
                self.assertEqual(set(candidate["plan"]), {"node_to_subgraph", "core_schedules"})
        self.assertEqual(before, ir.graph)

    def test_zero_cap_empty_graph_and_bad_partition_rejected(self):
        ir = ir_from_pairs(3, [(1, 2)])
        self.assertEqual(module.generate_convex_candidates(ir, 2, 0)[0], [])
        self.assertEqual(module.generate_convex_candidates(ir_from_pairs(0, []), 1)[0], [])
        for blocks in ([[1, 1], [2, 3]], [[1, 2], [99]], [[1, 2, 3], []]):
            with self.assertRaises(ValueError): module._scc_coarsen(ir, blocks, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
