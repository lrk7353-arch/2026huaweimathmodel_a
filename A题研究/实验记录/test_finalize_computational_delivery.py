"""Pure temporary-file/fake-process tests. Never calls official evaluation."""
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("finalizer_tested", Path(__file__).with_name("finalize_computational_delivery.py"))
f = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(f)


class Fixture:
    def __init__(self, root):
        self.root = Path(root)
        self.research = self.root / "A题研究"
        self.config = f.default_config(self.research, stable_seconds=0.001, interval=0.001)
        self.log = self.research / "实验记录"
        self.log.mkdir(parents=True)
        self.source = self.log / "frozen.py"
        self.source.write_text("# frozen fake source\n")
        for name in f.TOOLS:
            (self.log / name).write_text("# fake executable, never executed\n")
        self.data = self.root / "data"
        self.data.mkdir()
        graph_hashes = {}
        for i in range(1, 101):
            path = self.data / f"case_{i:03d}.json"
            f.write(path, {"case": i})
            graph_hashes[path.stem] = f.sha(path)
        config = self.data / "config.txt"
        config.write_text("fake config")
        benchmark = self.research / "legacy_benchmark"
        exploratory = self.research / "legacy_exploratory"
        benchmark.mkdir(); exploratory.mkdir()
        four = Path(self.config["four_cells"])
        four.mkdir()
        f.write(four / "launch.json", {"fake": True})
        f.write(self.config["snapshot_manifest"], {"benchmark_runs": [str(benchmark)], "exploratory_runs": [str(exploratory)]})
        specs = f.fixed_specs()
        for version in ("v2", "v3"):
            base = Path(self.config[version + "_root"])
            for name, (p, ns, seed) in specs[version].items():
                settings = {"cases": list(range(1, 101)), "problems": p if isinstance(p, list) else [p], "cores": ns, "seed": seed,
                            "data_dir": str(self.data), "config": str(config)}
                f.write(base / name / "manifest.json", {"settings": settings, "graphs_sha256": graph_hashes, "config_sha256": f.sha(config), "source_sha256": {},
                    "python": f.sys.version, "python_executable": str(Path(f.sys.executable).resolve())})
                slots = [{"case": c, "problem": p, "num_cores": n, "slot": f"{c}_p{p}_n{n}", "outcome": "completed_feasible",
                          "feasible": True, "search_completed": True, "logical_calls": 4, "evaluated_count": 4}
                         for c, p, n in sorted(f.graph_scope(settings))]
                f.write(base / name / "summary.json", {"planned_slots": len(slots), "reported_slots": len(slots), "all_slots_feasible": True,
                    "all_searches_completed": True, "source_hashes_unchanged": True, "slots": slots})
            for lane in "abc":
                p = "abc".index(lane) + 1
                if version == "v2":
                    if p == 1: names = ["component_p1_seed17", "singlecore_diagnostics_seed17"]
                    elif p == 2: names = ["component_p2_seed17", "operation_p2_n5_seed17", "full_p2_n5_seed29", "full_p2_n5_seed43"]
                    else: names = ["component_p3_n5_seed17", "operation_p3_n5_seed17", "trace_p3_n5_seed17", "full_p3_n5_seed29", "full_p3_n5_seed43"]
                else: names = [f"full_p{p}_seed17"] + ([f"full_p{p}_n5_seed{s}" for s in (29, 43)] if p > 1 else [])
                f.write(base / "queues" / (lane + ".manifest.json"), {"jobs": [{"name": n} for n in names], "wait_for": f"full_p{p}_seed17"})
                f.write(base / "queues" / (lane + ".done.json"), {"completed": True, "outcomes": [{"job": n, "exit_code": 0} for n in names]})
        f.write(four / "summary.json", {"all_four_cells_complete": True, "four_cell_complete_count": 400, "source_hashes_unchanged": True,
                "unresolved_calls": 0, "pair_controller_failures": [], "logical_cross_calls": 800, "cache_hits": 0,
                "pairs": [{"case": f"case_{i:03d}", "num_cores": n, "four_cell_complete": True} for i in range(1, 101) for n in (2, 3, 4, 5)]})
        f.write(four / "progress.json", {"phase": "finished"})
        (four / "four_cells.csv").write_text("fake fully-closed CSV\n")
        self.commands = []
        self.omit_slot = False
        self.inheritance_negative = False
        self.failure_stage = None
        self.mutate_stage = None
        self.raise_stage = None
        self.bad_inheritance = False
        self.fail_verify = False
        self.rejected_candidate = False

    def sources(self, config):
        return {"controller_declared": {}, "sources": {str(self.source): f.sha(self.source), str(self.log / "verify_delivery.py"): f.sha(self.log / "verify_delivery.py")}}

    def portfolio(self, directory):
        directory = Path(directory)
        keys = sorted(f.EXPECTED_KEYS)
        if self.omit_slot: keys = keys[:-1]
        rows = []
        for case, p, n in keys:
            path = directory / f"p{p}/n{n}" / (case + "_multicore_res.json")
            f.write(path, {"fake_plan": [case, p, n]})
            rows.append({"case": case, "problem": p, "num_cores": n, "plan_path": str(path), "plan_sha256": f.sha(path)})
        with (directory / "catalog.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
        f.write(directory / "manifest.json", {"selected_count": len(keys), "expected_full_coverage": 1500, "full_coverage": len(keys) == 1500,
            "rejected": [{"error": "fake corrupt candidate"}] if self.rejected_candidate else [], "coverage": dict(f.Counter(f"p{p}_n{n}" for _, p, n in keys))})

    def runner(self, command, stdout, stderr, heartbeat, timeout, interval):
        self.commands.append(command)
        name = Path(command[2]).name
        Path(stdout).write_text(""); Path(stderr).write_text("retained stderr\n")
        if self.mutate_stage == name:
            self.source.write_text("# changed\n"); heartbeat()
        if self.raise_stage == name:
            Path(stdout).write_text("partial evidence retained\n")
            raise RuntimeError("intentional fake process exception")
        if self.failure_stage == name: return 7
        option = lambda name: Path(command[command.index(name) + 1])
        if name == "summarize_formal.py":
            f.write(option("--out") / "summary.json", {"planned_slots": 3100, "verified_complete": 3100, "audit_errors": [], "aggregates": [{"all100": True}]})
        elif name == "summarize_v3.py":
            f.write(option("--output") / "summary.json", {"slot_count": 1600, "verified_slots": 1600, "audit_errors": [], "aggregates": [{"all_100": True}]})
        elif name == "collect_best_known.py":
            self.portfolio(option("--output"))
        elif name == "core_inheritance.py":
            out = option("--run-dir")
            proposal = {"case": "case_001", "num_cores": 2}
            f.write(out / "manifest.json", {"catalog_sha256": f.sha(command[3]), "logical_cap": 1200, "proposals": [proposal]})
            record = {"status": "timeout" if self.bad_inheritance else "success", "cache_hit": False}
            result = {"record": record, "accepted": not self.inheritance_negative}
            f.write(out / "case_001/p1_n2/pending.json", proposal)
            f.write(out / "case_001/p1_n2/result.json", result)
            f.write(out / "summary.json", {"sources_unchanged": True, "logical_calls": 1, "results": [result], "accepted": int(not self.inheritance_negative), "all_padding_times_equal": not self.inheritance_negative})
        elif name == "summarize_portfolio.py":
            f.write(Path(command[3]) / "metrics_summary.json", {"selected_count": 1500, "full_coverage": True,
                "groups": [{"all_100": True, "completed": 100} for _ in range(15)]})
        elif name == "make_delivery.py":
            out = option("--output"); out.mkdir()
            f.write(out / "delivery_manifest.json", {"fake": True, "selected_count": 1500})
            (out / "verify_delivery.py").write_bytes((self.log / "verify_delivery.py").read_bytes())
            f.write(stdout, {"status": "verified_complete", "selected_count": 1500, "missing_count": 0, "manifest_sha256": f.sha(out / "delivery_manifest.json")})
        elif name == "verify_delivery.py":
            f.write(stdout, {"status": "verified_partial" if self.fail_verify else "verified_complete", "selected_count": 1500,
                            "missing_count": 0, "manifest_sha256": f.sha(Path(command[3]) / "delivery_manifest.json"), "official_evaluations_performed": 0})
        else: raise AssertionError(name)
        return 0

    def run(self, **kw):
        return f.run_chain(self.config, runner=self.runner, source_provider=self.sources, sleep=lambda seconds: None,
                           records=lambda launch: {}, **kw)


class FinalizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.fx = Fixture(self.temp.name)

    def test_full_chain_fake_success_and_commands(self):
        result = self.fx.run()
        self.assertTrue(result["completed"], result)
        self.assertEqual(len(self.fx.commands), 8)
        self.assertEqual(result["coverage"]["selected_count"], 1500)
        self.assertEqual(result["calls"]["new_inheritance_logical_calls"], 1)
        for command in self.fx.commands: self.assertNotIn("--allow-partial", command)
        inherit = self.fx.commands[3]
        self.assertEqual(inherit[inherit.index("--max-evaluations") + 1], "1200")
        self.assertIn(self.fx.config["inheritance"], self.fx.commands[4])
        self.assertIn("--manifest-sha256", self.fx.commands[-1])
        self.assertEqual(result["dependencies"]["v2_complete_slots"], 3100)
        self.assertEqual(result["dependencies"]["v3_complete_slots"], 1600)
        for stage in result["stages"]:
            self.assertTrue(Path(stage["stdout"]).with_name("pending.json").exists())
            self.assertTrue(Path(stage["stdout"]).with_name("result.json").exists())

    def test_missing_1500_blocks_before_official_inheritance(self):
        self.fx.omit_slot = True
        result = self.fx.run()
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(len(self.fx.commands), 3)
        self.assertFalse(Path(self.fx.config["inheritance"]).exists())
        self.assertEqual(f.read(Path(self.fx.config["pre_portfolio"]) / "manifest.json")["selected_count"], 1499)

    def test_exception_retains_all_logs_and_no_retry(self):
        self.fx.raise_stage = "summarize_v3.py"
        result = self.fx.run()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(len(self.fx.commands), 2)
        self.assertIn("partial evidence", Path(result["stages"][-1]["stdout"]).read_text())
        self.assertFalse(result["automatic_retry"])
        with self.assertRaises(f.StopChain): self.fx.run()
        self.assertEqual(len(self.fx.commands), 2)

    def test_source_mutation_stops_during_fake_child(self):
        self.fx.mutate_stage = "summarize_formal.py"
        result = self.fx.run()
        self.assertEqual(result["state"], "source_changed")
        self.assertFalse(result["source_hashes_unchanged"])
        self.assertEqual(len(self.fx.commands), 1)

    def test_nonzero_child_retained_and_stops(self):
        self.fx.failure_stage = "summarize_formal.py"
        result = self.fx.run()
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(result["stages"][0]["exit_code"], 7)
        self.assertEqual(len(self.fx.commands), 1)

    def test_queue_needs_review_without_stages(self):
        p = Path(self.fx.config["v3_root"]) / "queues/b.needs_review.json"
        f.write(p, {"reason": "fake failed generation"})
        result = self.fx.run()
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(self.fx.commands, [])
        self.assertIn(str(p), result["error"])

    def test_final_four_cell_incomplete_blocks(self):
        p = Path(self.fx.config["four_cells"]) / "summary.json"
        s = f.read(p); s["four_cell_complete_count"] = 399; f.write(p, s)
        result = self.fx.run()
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(self.fx.commands, [])

    def test_wait_timeout_without_stages(self):
        (Path(self.fx.config["v3_root"]) / "queues/a.done.json").unlink()
        ticks = iter([0, 100000000])
        result = self.fx.run(monotonic=lambda: next(ticks))
        self.assertEqual(result["state"], "timed_out")
        self.assertEqual(self.fx.commands, [])
        self.assertTrue(result["dependencies"]["pending"])

    def test_existing_even_empty_output_rejected_before_launch(self):
        Path(self.fx.config["package"]).mkdir()
        with self.assertRaises(f.StopChain): self.fx.run()
        self.assertFalse(Path(self.fx.config["run_dir"]).exists())

    def test_inheritance_timeout_retained_with_accounting(self):
        self.fx.bad_inheritance = True
        result = self.fx.run()
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(len(self.fx.commands), 4)
        self.assertEqual(result["calls"]["new_inheritance_logical_calls"], 1)
        self.assertEqual(result["calls"]["inheritance_status_counts"], {"timeout": 1})
        self.assertFalse(Path(self.fx.config["portfolio"]).exists())

    def test_real_negative_inheritance_not_an_error(self):
        self.fx.inheritance_negative = True
        result = self.fx.run()
        self.assertTrue(result["completed"])
        self.assertEqual(result["calls"]["inheritance_accepted"], 0)
        self.assertFalse(result["stages"][3]["validation"]["all_padding_times_equal_observed"])

    def test_partial_packaged_verifier_not_success(self):
        self.fx.fail_verify = True
        result = self.fx.run()
        self.assertFalse(result["completed"])
        self.assertEqual(result["state"], "needs_review")
        self.assertTrue(Path(self.fx.config["package"]).exists())

    def test_crash_reservation_is_conservative_not_zero(self):
        root = Path(self.fx.config["inheritance"])
        f.write(root / "case_001/p1_n2/pending.json", {"reserved": True})
        state = f.inheritance_call_state(root)
        self.assertEqual(state["new_inheritance_logical_calls"], 1)
        self.assertEqual(state["inheritance_unresolved_reserved_calls"], 1)
        self.assertEqual(state["inheritance_completed_records"], 0)

    def test_source_stability_window_mutation_prevents_launch(self):
        def mutate(seconds): self.fx.source.write_text("changed at freeze")
        with self.assertRaises(f.StopChain):
            f.run_chain(self.fx.config, runner=self.fx.runner, source_provider=self.fx.sources, sleep=mutate)
        self.assertFalse(Path(self.fx.config["run_dir"]).exists())

    def test_rejected_candidate_blocks_even_full_coverage(self):
        self.fx.rejected_candidate = True
        result = self.fx.run()
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(len(self.fx.commands), 3)
        self.assertFalse(Path(self.fx.config["inheritance"]).exists())

    def test_source_changes_while_waiting_no_stage_runs(self):
        calls = []
        def probe(launch):
            calls.append(1)
            self.fx.source.write_text("source changed while dependency still pending")
            return {"ready": False, "pending": ["fake missing dependency"], "receipts": []}
        result = self.fx.run(probe=probe)
        self.assertEqual(result["state"], "source_changed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.fx.commands, [])

    def test_duplicate_matrix_cannot_hide_under_true_batch_flags(self):
        path = Path(self.fx.config["v3_root"]) / "full_p1_seed17/summary.json"
        value = f.read(path)
        value["slots"][-1] = value["slots"][0]
        f.write(path, value)
        result = self.fx.run()
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(self.fx.commands, [])

    def test_progress_slot_failure_stops_before_queue_done(self):
        root = Path(self.fx.config["v2_root"]) / "full_p1_seed17"
        (root / "summary.json").unlink()
        f.write(root / "progress.json", {"slots": [{"slot": "case_001_p1_n2", "outcome": "slot_failed"}]})
        result = self.fx.run()
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(self.fx.commands, [])

    def test_runtime_mismatch_blocks_before_launch(self):
        path = Path(self.fx.config["v2_root"]) / "full_p1_seed17/manifest.json"
        value = f.read(path); value["python"] = "different runtime"; f.write(path, value)
        with self.assertRaises(f.StopChain): self.fx.run()
        self.assertFalse(Path(self.fx.config["run_dir"]).exists())

    def test_four_cell_summary_precedes_csv_is_pending_not_failed(self):
        launch = f.freeze(self.fx.config, source_provider=self.fx.sources, sleep=lambda seconds: None)
        path = Path(self.fx.config["four_cells"]) / "progress.json"
        f.write(path, {"phase": "evaluating"})
        result = f.probe_dependencies(launch)
        self.assertFalse(result["ready"])
        self.assertIn(str(path) + "#phase=finished", result["pending"])
        f.write(path, {"phase": "finished"})
        self.assertTrue(f.probe_dependencies(launch)["ready"])

    def test_four_cell_controller_error_stops_without_summary(self):
        root = Path(self.fx.config["four_cells"])
        (root / "summary.json").unlink()
        f.write(root / "controller_error.json", {"error": "fake crash"})
        result = self.fx.run()
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(self.fx.commands, [])


if __name__ == "__main__":
    unittest.main()
