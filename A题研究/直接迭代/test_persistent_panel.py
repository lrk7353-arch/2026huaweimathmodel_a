"""Accounting tests using synthetic records only; no official solver executions."""
import csv
import gzip
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from common_run import atomic_json
from run_persistent_panel import frozen_protocol, validate_protocol, validate_complete, completed_attempt
from summarize_persistent_panel import summarize


def record(directory, index, makespan, copy):
    path = directory / f"evaluation_{index:02d}"
    path.mkdir(parents=True)
    plan = {"node_to_subgraph": {"1": 0}, "core_schedules": [[0]], "test_identity": index}
    atomic_json(path / "plan.json", plan)
    digest = hashlib.sha256((path / "plan.json").read_bytes()).hexdigest()
    r = dict(status="success", attempt_id=str(path), record_path=str(path / "record.json"),
             plan_path=str(path / "plan.json"), problem=3, cache_hit=False, elapsed_seconds=.5,
             hashes=dict(plan_sha256=digest),
             metrics=dict(makespan=makespan, data_movement_bytes=dict(added_copy_bytes=copy),
                          cache_stats=dict(hit_bytes=10, miss_bytes=30)))
    atomic_json(path / "record.json", r)
    return r


class FrozenPanelTests(unittest.TestCase):
    def test_frozen_selection_and_order(self):
        p = frozen_protocol()
        self.assertEqual(p, frozen_protocol())
        validate_protocol(p)
        self.assertEqual(len(p["configurations"]), 75)
        self.assertEqual(len(p["frozen_regression_cases"]), 4)
        self.assertEqual(len(p["regression_pool"]), 20)
        for problem in (1, 2, 3):
            self.assertEqual(len([c for c in p["configurations"]
                                  if c["problem"] == problem and c["cores"] == 1]), 2)
            orders = [c["arm_order"][0] for c in p["configurations"]
                      if c["problem"] == problem and c["cores"] == 5]
            self.assertLessEqual(max(orders.count(x) for x in p["variants"]) -
                                 min(orders.count(x) for x in p["variants"]), 2)

    def test_complete_requires_paid_best_and_intact_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a, b = record(root, 1, 100, 200), record(root, 2, 90, 300)
            summary = dict(complete=True, calls=[dict(record=a), dict(record=b)], logical_calls=2, best_record=a)
            with self.assertRaisesRegex(ValueError, "best paid"):
                validate_complete(summary, 24)
            summary["best_record"] = b
            self.assertTrue(validate_complete(summary, 24))
            Path(b["plan_path"]).write_text("{}")
            with self.assertRaisesRegex(ValueError, "changed"):
                validate_complete(summary, 24)

    def test_export_tradeoffs_initialization_and_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, target = root / "source", root / "export"
            protocol = frozen_protocol()
            config = dict(id="case_001_p3_n1", case=1, problem=3, cores=1,
                          group="core_regression", arm_order=["persistent", "legacy", "mature"])
            protocol["configurations"] = [config]
            atomic_json(source / "protocol.json", protocol)
            for variant in protocol["variants"]:
                arm = source / "configurations" / config["id"] / variant
                if variant == "persistent":
                    old = arm / "attempt_001"
                    r = record(old, 0, 120, 60)
                    atomic_json(old / "progress.json", dict(calls=[dict(record=r)], complete=False))
                attempt = arm / ("attempt_002" if variant == "persistent" else "attempt_001")
                a, b, c = record(attempt, 1, 100, 200), record(attempt, 2, 101, 100), record(attempt, 3, 104, 1)
                summary = dict(complete=True, logical_calls=3, calls=[dict(record=a, phase="initialization"),
                    dict(record=b, phase="initialization"), dict(record=c, lineage=0, parent_depth=1)],
                    best_record=a, initialization_calls=2, initialization_plan_hashes=["a", "b"],
                    generation_seconds=.25, elapsed_seconds=2.,
                    branch_events=[dict(event="advance", lineage=0, depth=1)])
                atomic_json(attempt / "summary.json", summary)
                self.assertIsNotNone(completed_attempt(arm, 24))
            result = summarize(source, target)
            self.assertEqual(result["completed_arms"], 3)
            self.assertEqual(result["calls"], 9)
            self.assertEqual(result["total_recorded_calls_including_recovery"], 10)
            self.assertEqual(result["first_attempt_valid_arms"], 2)
            self.assertEqual(result["initialization_mismatches"], 0)
            with (target / "per_arm.csv").open(encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertTrue(all(float(r["statement_speedup"]) == 1 for r in rows))
            with (target / "paid_copy_tradeoffs.csv").open(encoding="utf-8-sig") as f:
                trades = list(csv.DictReader(f))
            allowed = [r for r in trades if r["variant"] == "persistent" and float(r["allowed_time_regression"]) == .03]
            self.assertEqual(int(allowed[0]["makespan"]), 101)
            self.assertEqual(int(allowed[0]["added_copy"]), 100)
            with gzip.open(target / "all_paid_calls_and_recovery.json.gz", "rt") as f:
                evidence = json.load(f)
            self.assertEqual(len(evidence), 3)
            with tarfile.open(target / "all_evaluated_plans.tar.gz") as archive:
                self.assertEqual(len(archive.getmembers()), 4)


if __name__ == "__main__":
    unittest.main()
