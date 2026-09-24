"""Mechanism tests use saved official observations; no evaluator is invoked."""
import copy
import gzip
import json
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parents[1]
RESEARCH = HERE.parent
sys.path.insert(0, str(HERE))
from cache_refine import CacheResultMismatch, generate_cache_candidates
from graph_ir import GraphIR
from plan import validate_plan


def fixture(name):
    base = RESEARCH / "方案审阅" / "cache_microtests" / name
    return (GraphIR.from_path(str(base) + "_graph.json"),
            json.loads(Path(str(base) + "_plan.json").read_text()),
            json.loads(Path(str(base) + "_result.json").read_text()))


def fifo_mock():
    """Explicit event replay fixture, NOT an official performance observation."""
    graph = {"tensors": [{"id": i, "pos": "L1", "size": 400000} for i in (1, 2, 3)],
             "ops": [{"id": i, "op": "ADD", "pipe": "PIPE_M", "cycles": 10} for i in (100, 101, 102, 103)],
             "edges": [{"source": tid, "target": op} for tid, op in ((1, 100), (2, 101), (3, 102), (1, 103))]}
    plan = {"node_to_subgraph": {str(op): sg for sg, op in enumerate((100, 101, 102, 103))},
            "core_schedules": [[0, 1, 2, 3]]}
    events, ops = [], []
    for sg, (tid, op) in enumerate(((1, 100), (2, 101), (3, 102), (1, 103))):
        now, copy_id = sg * 20, 200 + sg
        ops += [{"op_id": copy_id, "op": "COPY_IN", "pipe": "PIPE_MTE2", "subgraph_id": sg,
                 "start": now, "end": now + 10, "duration": 10, "cache_tensor_id": tid,
                 "cache_hit": False, "memory_path": "DDR"},
                {"op_id": op, "op": "ADD", "pipe": "PIPE_M", "subgraph_id": sg,
                 "start": now + 10, "end": now + 20, "duration": 10}]
        events += [{"time": now, "event": "miss", "tensor_id": tid, "size_bytes": 400000,
                    "core_id": 0, "op_id": copy_id},
                   {"time": now + 10, "event": "insert", "tensor_id": tid, "size_bytes": 400000,
                    "used_bytes": 400000 if sg == 0 else 800000,
                    "evicted_tensor_ids": [] if sg < 2 else [sg - 1], "core_id": 0, "op_id": copy_id}]
    result = {"problem": 3, "cache_mode": "read_only", "num_cores": 1, "makespan": 80,
              "bandwidth_bytes_per_cycle": 60, "cache_capacity_bytes": 1048576,
              "cache_bandwidth_bytes_per_cycle": 250, "cross_core_copy_delay_cycles": 500,
              "capacity_bytes": {"L1": 524288, "UB": 131072},
              "per_core_timeline": [{"core_id": 0, "ops": ops}], "cache_events": events,
              "cache_stats": {"copy_in_hits": 0, "copy_in_misses": 4, "hit_bytes": 0,
                              "miss_bytes": 1600000, "hit_rate": 0.0},
              "cache_used_bytes_final": 800000,
              "cache_final_entries": [{"tensor_id": i, "size_bytes": 400000} for i in (3, 1)],
              "cross_core_transfers": []}
    return GraphIR.from_graph(graph), plan, result


class CacheRefineTests(unittest.TestCase):
    def test_all_five_official_microtests_and_immutability(self):
        expected = {"simultaneous_cold_reads": 410, "short_compute_simultaneous": 313,
                    "short_compute_staggered": 235, "long_follower_compute_simultaneous": 3211,
                    "long_follower_compute_staggered": 3225}
        for name, makespan in expected.items():
            with self.subTest(name=name):
                ir, plan, result = fixture(name)
                before = copy.deepcopy((ir.graph, plan, result))
                candidates, diag = generate_cache_candidates(ir, plan, result, num_cores=2)
                self.assertEqual(result["makespan"], makespan)
                self.assertTrue(candidates[0]["metadata"]["is_reencoding_control"])
                self.assertTrue(diag["no_evaluation_performed"])
                self.assertEqual(before, (ir.graph, plan, result))
                for candidate in candidates:
                    self.assertTrue(validate_plan(ir, candidate["plan"]))
                    self.assertEqual(len(candidate["plan"]["core_schedules"]), 2)

    def test_cold_misses_and_same_cycle_insert_before_hit(self):
        ir, plan, result = fixture("short_compute_simultaneous")
        _, diag = generate_cache_candidates(ir, plan, result, num_cores=2)
        self.assertEqual(diag["cache_access_counts"], {"first_access_miss": 2, "concurrent_cold_miss": 1})
        ir, plan, result = fixture("short_compute_staggered")
        candidates, diag = generate_cache_candidates(ir, plan, result, num_cores=2)
        self.assertEqual(diag["cache_access_counts"], {"first_access_miss": 2, "hit": 1})
        hit = next(e for e in result["cache_events"] if e["event"] == "hit")
        self.assertTrue(any(e["event"] == "insert" and e["time"] == hit["time"] and
                            e["tensor_id"] == hit["tensor_id"] for e in result["cache_events"]))

    def test_useful_work_reordering_is_actually_encoded(self):
        ir, plan, result = fixture("short_compute_simultaneous")
        candidates, _ = generate_cache_candidates(ir, plan, result, num_cores=2)
        candidate = next(c for c in candidates if c["metadata"]["mechanism"] == "advance_existing_independent_work_before_follower")
        self.assertEqual(candidate["metadata"]["changed_core_count"], 0)
        new_plan = candidate["plan"]
        core_order = new_plan["core_schedules"][1]
        self.assertLess(core_order.index(new_plan["node_to_subgraph"]["202"]),
                        core_order.index(new_plan["node_to_subgraph"]["201"]))

    def test_budget_determinism_and_rounds(self):
        ir, plan, result = fixture("short_compute_simultaneous")
        for budget in (1, 2, 12):
            for round_index in (0, 1):
                args = dict(num_cores=2, max_candidates=budget, round_index=round_index, seed=17)
                first = generate_cache_candidates(ir, plan, result, **args)
                self.assertEqual(first, generate_cache_candidates(ir, plan, result, **args))
                self.assertLessEqual(len(first[0]), budget)

    def test_reject_graph_plan_result_and_config_mismatches(self):
        mutations = [lambda r: r.update(cache_capacity_bytes=999),
                     lambda r: r.update(num_cores=1),
                     lambda r: r.update(makespan=r["makespan"] + 1),
                     lambda r: r["cache_events"][0].update(tensor_id=999999),
                     lambda r: r["cache_events"][0].update(size_bytes=1),
                     lambda r: r["cache_events"][0].update(event="hit"),
                     lambda r: r["cache_stats"].update(hit_bytes=100),
                     lambda r: r["cache_final_entries"].reverse(),
                     lambda r: r["cache_events"].append(copy.deepcopy(r["cache_events"][-1]))]
        for mutation in mutations:
            ir, plan, result = fixture("short_compute_staggered")
            mutation(result)
            with self.assertRaises(CacheResultMismatch):
                generate_cache_candidates(ir, plan, result, num_cores=2)
        ir, plan, result = fixture("short_compute_simultaneous")
        plan["core_schedules"] = [plan["core_schedules"][1], plan["core_schedules"][0]]
        with self.assertRaises(CacheResultMismatch):
            generate_cache_candidates(ir, plan, result, num_cores=2)

    def test_fifo_post_eviction_classification_and_order(self):
        ir, plan, result = fifo_mock()
        _, diag = generate_cache_candidates(ir, plan, result, num_cores=1)
        self.assertEqual(diag["cache_access_counts"], {"first_access_miss": 3, "post_eviction_miss": 1})
        self.assertEqual(diag["fifo_eviction_count"], 2)
        result["cache_events"][5]["evicted_tensor_ids"] = [2]
        with self.assertRaisesRegex(CacheResultMismatch, "FIFO"):
            generate_cache_candidates(ir, plan, result, num_cores=1)

    def test_inactive_cores_up_to_five(self):
        for n in range(1, 6):
            ir, plan, result = fifo_mock()
            plan["core_schedules"].extend([] for _ in range(n - 1))
            result["num_cores"] = n
            result["per_core_timeline"].extend({"core_id": i, "ops": []} for i in range(1, n))
            candidates, _ = generate_cache_candidates(ir, plan, result, num_cores=n)
            self.assertTrue(all(len(c["plan"]["core_schedules"]) == n for c in candidates))

    def test_real_cross_core_routes_use_original_tensors_and_500_delay(self):
        source = RESEARCH / "实验记录" / "independent_local_move_check" / "verification.json"
        if not source.exists():
            self.skipTest("archived case071 observation is unavailable")
        record = json.loads(source.read_text())["evaluations"]["3"]
        with gzip.open(record["result_path"], "rt") as handle:
            result = json.load(handle)
        ir = GraphIR.from_path(record["graph_path"])
        plan = json.loads(Path(record["plan_path"]).read_text())
        _, diag = generate_cache_candidates(ir, plan, result, num_cores=5, max_candidates=1)
        self.assertEqual(diag["cross_routes_verified"], 156)
        result["cross_core_transfers"][0]["copy_in_release"] += 1
        with self.assertRaisesRegex(CacheResultMismatch, "500"):
            generate_cache_candidates(ir, plan, result, num_cores=5)


if __name__ == "__main__":
    unittest.main()
