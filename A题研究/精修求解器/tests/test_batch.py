"""Batch orchestration/evidence tests; synthetic child files, zero evaluators."""
import copy
import gzip
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import batch


class FakeChild:
    def __init__(self, mode="success", delay=0):
        self.mode, self.delay = mode, delay
        self.commands, self.active, self.maximum = [], {}, 0
        self.lock = threading.Lock()

    def __call__(self, command, **kwargs):
        attempt = Path(command[command.index("--run-dir") + 1])
        if attempt.exists(): raise AssertionError("batch must leave the strict fresh solver directory absent")
        launch = batch.read_json(attempt.parent / (attempt.name + ".launch.json"))
        s = launch["signature"]; case = Path(s["graph_path"]).stem
        with self.lock:
            if self.active.get(case): raise AssertionError("same graph ran concurrent slots")
            self.active[case] = True
            self.maximum = max(self.maximum, sum(self.active.values()))
            self.commands.append(command)
        try:
            if self.delay: time.sleep(self.delay)
            attempt.mkdir()
            mode = self.mode.get(case, "success") if isinstance(self.mode, dict) else self.mode
            if mode == "crash": return subprocess.CompletedProcess(command, 2)
            if mode == "raise": raise OSError("synthetic launch error")
            cores, problem = s["num_cores"], s["problem"]
            plan = {"node_to_subgraph": {"1": 0}, "core_schedules": [[0]] + [[] for _ in range(cores - 1)]}
            if s["incumbent_path"]: plan = batch.read_json(s["incumbent_path"])
            trial_plan = attempt / "trials/0000.plan.json"
            batch.atomic_json(trial_plan, plan)
            plan_path = attempt / "eval/plan.json"; plan_path.parent.mkdir()
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, separators=(",", ":")))
            movements = dict(original_graph_copy_bytes=0, scheduled_copy_bytes=0, added_copy_bytes=0,
                             partition_added_copy_bytes=0, spill_added_copy_bytes=0)
            timeline = []
            assigned = next(k for k, seq in enumerate(plan["core_schedules"]) if seq)
            for k in range(cores):
                tasks = ([{"subgraph_id": 0, "start": 0, "end": 6}] if k == assigned else []) if problem == 1 else [
                    {"subgraph_ids": [0] if k == assigned else [], "start": 0, "end": 6 if k == assigned else 0}]
                timeline.append({"core_id": k, "tasks": tasks, "ops": [dict(task_id=0, subgraph_id=0,
                    op_id=1, op="MATMUL", pipe="PIPE_M", start=0, end=6, duration=6)] if k == assigned else []})
            raw = dict(scene="A" if problem == 1 else "B", num_cores=cores, makespan=6,
                bandwidth_bytes_per_cycle=60, capacity_bytes=dict(L1=524288, UB=131072),
                input_graph=Path(s["graph_path"]).name, input_plan=plan_path.name,
                data_movement_bytes=movements, per_core_timeline=timeline)
            if problem == 1: raw.update(task_cross_core_wait_cycles=1000, task_same_core_wait_cycles=100)
            else: raw.update(cross_core_copy_delay_cycles=500)
            if problem == 3: raw.update(problem=3, cache_mode="read_only", cache_capacity_bytes=1048576,
                                        cache_bandwidth_bytes_per_cycle=250, cache_stats={"hits": 0})
            if mode == "bad_duration": raw["per_core_timeline"][assigned]["ops"][0]["end"] = 5
            result_path = attempt / "eval/official_result.json.gz"
            with gzip.open(result_path, "wt") as f: json.dump(raw, f)
            record = dict(status="success", problem=problem, returncode=0, cache_hit=False,
                plan_path=str(plan_path), result_path=str(result_path), result_sha256=batch.digest(result_path),
                metrics=dict(makespan=6, num_cores=cores, data_movement_bytes=movements),
                hashes=dict(graph_sha256=s["graph_sha256"], config_sha256=s["config_sha256"],
                    plan_sha256=batch.digest(plan_path), problem=problem,
                    official_py_sha256={Path(p).name: h for p,h in s["source_sha256"].items() if Path(p).parent==batch.OFFICIAL},
                    wrapper_sha256=s["source_sha256"][str(batch.RESEARCH/"solver/evaluator.py")],
                    worker_sha256=s["source_sha256"][str(batch.RESEARCH/"solver/eval_worker.py")],
                    python=s["python"], python_executable=s["python_executable"]))
            if problem == 3: record["metrics"]["cache_stats"] = raw["cache_stats"]
            trial = dict(index=0, stage="initial" if s["incumbent_path"] else "component", name="fixture",
                state="returned", accepted=True, plan_path=str(trial_plan), plan_file_sha256=batch.digest(trial_plan),
                plan_sha256=batch.object_digest(plan), record=record)
            output = attempt / "best.plan.json"; batch.atomic_json(output, plan)
            result = {**s, "evaluation_dir": s["shared_evaluation_dir"] or str(attempt/"evaluations"),
                "status": "success", "completed": mode != "unfinished", "state": "finished" if mode != "unfinished" else "aborted",
                "source_and_input_hashes_verified": mode != "unfinished", "test_hooks_used": False,
                "evaluations": [trial], "logical_calls": 1, "returned_calls": 1, "pending_calls": 0,
                "stage_calls": {trial["stage"]:1}, "status_counts": {"success":1}, "cache_hits": 0,
                "confirmed_worker_calls":1, "generation_failures": ["synthetic failure"] if mode == "generation_error" else [],
                "rejected_candidates": ["synthetic rejection"] if mode == "rejected" else [],
                "controller_error": None, "requires_review": mode in ("generation_error", "rejected"),
                "best": {**trial, "plan": plan}, "output": str(output), "output_sha256": batch.digest(output),
                "stop_reason": "fixture_complete"}
            batch.atomic_json(attempt / "summary.json", result)
            return subprocess.CompletedProcess(command, 1 if mode == "unfinished" else 0)
        finally:
            with self.lock: self.active[case] = False


class BatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve(); self.data = self.base / "attachments/data"; self.data.mkdir(parents=True)
        for i in (1,2):
            batch.atomic_json(self.data / f"case_{i:03d}.json", {"ops":[{"id":1,"op":"MATMUL","pipe":"PIPE_M","cycles":6}],"tensors":[],"edges":[]})
        (self.data / "config.txt").write_text("fixture config")
        research = self.base / "research"; (research / "solver").mkdir(parents=True)
        official = self.base / "attachments/code"; official.mkdir()
        self.entry = research / "solve.py"; self.entry.write_text("# synthetic child is injected; never executed\n")
        self.sources = {}
        for p in (self.entry, research/"controller.py", research/"solver/evaluator.py", research/"solver/eval_worker.py", official/"fixture.py"):
            if not p.exists(): p.write_text("# fixture\n")
            self.sources[str(p)] = batch.digest(p)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(batch, "OFFICIAL", official).start()
        mock.patch.object(batch, "RESEARCH", research).start()
        mock.patch.object(batch, "source_hashes", side_effect=lambda:dict(self.sources)).start()
        # The frozen controller verifier has its own mechanism tests/real smoke.
        # These fixtures exercise the independent batch checks and orchestration.
        mock.patch.object(batch.controller, "verify_summary", side_effect=lambda p,**kw:batch.read_json(p)).start()
        self.settings = dict(cases=[1],problems=[2],cores=[2],data_dir=str(self.data),config=str(self.data/"config.txt"),
            workers=1,seed=17,caps=dict(batch.CAP_DEFAULTS),round_width=6,max_rounds=5,max_evaluations=4,
            timeout=None,wcc_policy="mixed",evaluation_dir=None,incumbent_plan=None)

    def run_fixture(self, runner, settings=None, out=None, resume=False):
        return batch.run_batch(settings or self.settings, out or self.base/"run", resume=resume,runner=runner,solve_entry=self.entry)

    def test_selection_defaults_and_rejections(self):
        self.assertEqual(batch.parse_numbers("071,1,case_071",100,allow_all=True),[71,1])
        self.assertEqual(len(batch.parse_numbers("all",100,allow_all=True)),100)
        for raw in ("", "0", "101", "1,,2", "一", "1;2"):
            with self.assertRaises(ValueError): batch.parse_numbers(raw,100,allow_all=True)

    def test_completed_resume_reuses_without_any_child_call(self):
        child=FakeChild(); first=self.run_fixture(child); second=self.run_fixture(child,resume=True)
        self.assertEqual(first["completed_feasible"],1);self.assertEqual(second["reused_count"],1)
        self.assertEqual(len(child.commands),1);self.assertTrue(second["source_hashes_unchanged"])

    def test_failed_retry_uses_new_attempt_and_preserves_old_launch(self):
        child=FakeChild("crash");first=self.run_fixture(child);old=Path(first["slots"][0]["attempt_dir"])
        launch=old.parent/(old.name+".launch.json");before=launch.read_bytes();child.mode="success"
        second=self.run_fixture(child,resume=True)
        self.assertEqual(second["completed_feasible"],1);self.assertTrue(second["slots"][0]["attempt_dir"].endswith("attempt_0002"))
        self.assertEqual(launch.read_bytes(),before);self.assertTrue(old.exists())

    def test_corrupt_plan_or_gzip_is_never_reused_and_gets_new_attempt(self):
        for kind in ("plan", "gzip", "published"):
            out=self.base/kind;child=FakeChild();first=self.run_fixture(child,out=out);attempt=Path(first["slots"][0]["attempt_dir"])
            target=attempt/({"plan":"eval/plan.json","gzip":"eval/official_result.json.gz","published":"best.plan.json"}[kind]);target.write_bytes(b"corrupt")
            second=self.run_fixture(child,out=out,resume=True)
            self.assertEqual(len(child.commands),2);self.assertEqual(second["completed_feasible"],1)
            self.assertEqual(target.read_bytes(),b"corrupt");self.assertEqual(second["reused_count"],0)

    def test_summary_or_runtime_source_input_parameter_mismatch_is_hard_failure(self):
        child=FakeChild();self.run_fixture(child)
        for field,value in (("max_evaluations",5),("workers",2),("seed",29)):
            s=copy.deepcopy(self.settings);s[field]=value
            with self.assertRaises(batch.IntegrityError):self.run_fixture(child,settings=s,resume=True)
        for p in (self.data/"case_001.json",self.data/"config.txt"):
            before=p.read_bytes();p.write_bytes(before+b" ")
            with self.assertRaises(batch.IntegrityError):self.run_fixture(child,resume=True)
            p.write_bytes(before)
        old=self.sources[str(self.entry)];self.sources[str(self.entry)]="different"
        with self.assertRaises(batch.IntegrityError):self.run_fixture(child,resume=True)
        self.sources[str(self.entry)]=old;self.assertEqual(len(child.commands),1)

    def test_full_matrix_keeps_failures_and_no_partial_mean(self):
        s=copy.deepcopy(self.settings);s["cases"]=[1,2];child=FakeChild({"case_002":"crash"})
        result=self.run_fixture(child,settings=s)
        self.assertEqual((result["planned_slots"],len(result["slots"]),result["completed_feasible"],result["failed"]),(2,2,1,1))
        self.assertFalse(result["all_slots_feasible"]);self.assertTrue(result["no_partial_performance_mean_computed"])

    def test_generation_bug_requires_review_even_with_verified_best(self):
        for mode in ("generation_error","rejected"):
            out=self.base/mode;child=FakeChild(mode);r=self.run_fixture(child,out=out)
            self.assertEqual(r["requires_review_count"],1);self.assertEqual(r["failed"],1)
            self.assertTrue(r["all_slots_feasible"]);self.assertFalse(r["all_searches_completed"])
            child.mode="success";r2=self.run_fixture(child,out=out,resume=True)
            self.assertEqual(len(child.commands),2);self.assertEqual(r2["completed_feasible"],1)

    def test_unfinished_or_wrong_raw_timeline_cannot_complete(self):
        for mode in ("unfinished","bad_duration"):
            r=self.run_fixture(FakeChild(mode),out=self.base/mode)
            self.assertEqual(r["failed"],1);self.assertFalse(r["all_searches_completed"])

    def test_parallelism_is_graph_level_and_slot_order_is_preserved(self):
        s=copy.deepcopy(self.settings);s.update(cases=[1,2],problems=[3,1],cores=[2,1],workers=2)
        child=FakeChild(delay=.015);r=self.run_fixture(child,settings=s)
        self.assertEqual(r["completed_feasible"],8);self.assertEqual(child.maximum,2)
        for case in ("case_001","case_002"):
            commands=[c for c in child.commands if Path(c[3]).stem==case]
            self.assertEqual([(int(c[c.index("-p")+1]),int(c[c.index("-n")+1])) for c in commands],[(3,2),(3,1),(1,2),(1,1)])

    def test_every_search_parameter_is_forwarded_and_default_is_cold(self):
        s=copy.deepcopy(self.settings);s.update(round_width=3,max_rounds=7,wcc_policy="protected",timeout=72.5)
        s["caps"]={k:i for i,k in enumerate(batch.CAP_DEFAULTS)};child=FakeChild();self.run_fixture(child,settings=s)
        command=child.commands[0]
        self.assertNotIn("--incumbent-plan",command)
        for key,value in (("--round-width",3),("--max-rounds",7),("--timeout",72.5),("--wcc-policy","protected")):
            self.assertEqual(command[command.index(key)+1],str(value))
        for stage,cap in s["caps"].items():self.assertEqual(command[command.index(f"--{stage}-cap")+1],str(cap))

    def test_timeout_boundary_and_explicit_override(self):
        m=batch.make_manifest(self.settings,self.entry)
        self.assertEqual(batch.slot_signature(m,"case_001",2,2)["timeout"],60)
        m["compute_ops_by_case"]["case_001"]=10001
        self.assertEqual(batch.slot_signature(m,"case_001",2,2)["timeout"],180)
        m["settings"]["timeout"]=77
        self.assertEqual(batch.slot_signature(m,"case_001",2,2)["timeout"],77)

    def test_explicit_incumbent_only_one_slot(self):
        p=self.base/"initial.json";batch.atomic_json(p,{"node_to_subgraph":{"1":0},"core_schedules":[[0],[]]})
        s=copy.deepcopy(self.settings);s["incumbent_plan"]=str(p);child=FakeChild();r=self.run_fixture(child,settings=s)
        self.assertEqual(r["completed_feasible"],1);self.assertIn("--incumbent-plan",child.commands[0])
        s["cases"]=[1,2]
        with self.assertRaises(ValueError):batch.validate_settings(s)

    def test_active_orphan_child_is_pending_not_relaunched(self):
        child=FakeChild("crash");r=self.run_fixture(child);attempt=Path(r["slots"][0]["attempt_dir"])
        (attempt.parent/(attempt.name+".exit.json")).unlink()
        batch.atomic_json(attempt.parent/(attempt.name+".process.json"),{"pid":123})
        with mock.patch.object(batch,"_pid_alive",return_value=True):resumed=self.run_fixture(child,resume=True)
        self.assertEqual(resumed["pending"],1);self.assertEqual(len(child.commands),1)

    def test_signature_tampering_or_untracked_attempt_never_overwrites(self):
        child=FakeChild();r=self.run_fixture(child);attempt=Path(r["slots"][0]["attempt_dir"])
        launch=attempt.parent/(attempt.name+".launch.json");d=batch.read_json(launch);d["command"].append("--altered");batch.atomic_json(launch,d)
        resumed=self.run_fixture(child,resume=True)
        self.assertEqual(len(child.commands),1);self.assertTrue(resumed["fatal_errors"])
        self.assertFalse(resumed["all_searches_completed"])


if __name__ == "__main__": unittest.main()
