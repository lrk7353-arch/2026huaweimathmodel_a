#!/usr/bin/env python3
"""Preregistered saved width1 plans, P1/N5, cap2 and necessary-DDR pruning.

Independent extra-budget experiment; no regeneration, no frozen-v3 edits.
Prepare once, then execute exactly that manifest in the same fresh directory.
"""
from pathlib import Path
from types import SimpleNamespace
import argparse
import sys
import time
import traceback

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import controller
from solver.common import DATA, atomic_json, digest, object_digest, read_json
from solver.graph_ir import GraphIR
from solver.plan import validate_plan
from solver.evaluator import evaluate
from p1_boundary_lower_bound import boundary_ddr_lower_bound


def sources():
    result = controller.source_hashes()
    for name in ("p1_convex_regions.py", "p1_depth_bands.py", "p1_boundary_lower_bound.py",
                 "analyze_p1_convex.py", "analyze_p1_depth_bands.py", "probe_p1_depth.py"):
        p = HERE / name
        result[str(p)] = digest(p)
    return result


def view(row, manifest):
    return SimpleNamespace(ir=GraphIR.from_path(row["graph_path"]), graph_path=Path(row["graph_path"]),
        num_cores=5, problem=1, manifest=dict(source_sha256=manifest["source_sha256"],
        graph_sha256=row["graph_sha256"], config_sha256=manifest["config_sha256"]))


def integrity(manifest):
    if sources() != manifest["source_sha256"]:
        raise ValueError("frozen dependency changed")
    for p, h in manifest["read_only_evidence_sha256"].items():
        if digest(p) != h:
            raise ValueError("frozen input/evidence changed: " + p)


def prepare(out):
    if out.exists() or out.is_symlink():
        raise ValueError("prepare requires a strictly fresh run directory")
    config = DATA / "config.txt"
    controller.refinement_helpers.check_fixed_config(config)
    structure = HERE / "runs/p1_depth_structure_v1"
    replay = HERE.parent / "实验记录/p1_selective_v1/independent_replay"
    rows, files = [], {str(config): digest(config)}
    frozen = sources()
    for case, expected_baseline in (("003", 419601), ("016", 7715523)):
        graph = DATA / ("case_" + case + ".json")
        candidate = structure / ("case_" + case) / "width1.plan.json"
        verification_path = replay / ("case_" + case) / "verification.json"
        replay_verification = read_json(verification_path)
        if not all(replay_verification[k] for k in ("fresh", "all_raw_fields_equal", "input_hashes_equal")):
            raise ValueError("baseline was not independently replayed")
        baseline = replay_verification["record"]
        baseline_plan = read_json(baseline["plan_path"])
        original_summary = replay.parent / ("case_" + case) / "summary.json"
        original_best = read_json(original_summary)["best"]
        if object_digest(original_best["plan"]) != object_digest(baseline_plan):
            raise ValueError("baseline replay plan differs from selected plan")
        if controller.objective(baseline)[0] != expected_baseline:
            raise ValueError("baseline differs from authorized preregistration")
        row = dict(case=case, graph_path=str(graph), graph_sha256=digest(graph),
            candidate_path=str(candidate), candidate_file_sha256=digest(candidate),
            candidate_plan_sha256=object_digest(read_json(candidate)),
            baseline=baseline, baseline_objective=list(controller.objective(baseline)),
            baseline_verification_path=str(verification_path),
            original_summary_path=str(original_summary))
        preview = dict(source_sha256=frozen, config_sha256=digest(config))
        v = view(row, preview)
        controller.Solver.verify_success(v, baseline, baseline_plan, object_digest(baseline_plan))
        candidate_plan = read_json(candidate)
        validate_plan(v.ir, candidate_plan)
        if len(candidate_plan["core_schedules"]) != 5:
            raise ValueError("candidate core count differs")
        lb = boundary_ddr_lower_bound(v.ir, candidate_plan)
        row["boundary_lower_bound"] = lb
        prune_lb = lb["conservative_compute_cross_task_subset"]["nominal_ddr_cycles"]
        row["prune_lower_bound"] = prune_lb
        row["decision"] = "strict_ddr_bound_pruned" if prune_lb > expected_baseline else "evaluate"
        row["prune_reason"] = ("mandatory original compute-to-compute cross-Task COPY nominal DDR work strictly exceeds verified incumbent makespan"
                               if row["decision"] != "evaluate" else None)
        for p in (graph, candidate, verification_path, original_summary, Path(baseline["record_path"]),
                  Path(baseline["plan_path"]), Path(baseline["result_path"]), structure / "manifest.json"):
            files[str(p)] = digest(p)
        rows.append(row)
    manifest = dict(schema_version=1, experiment="p1_depth_width1_v1", problem=1, num_cores=5,
        scope="independent additional-budget candidate probe; not a formal-v3/fair-budget result",
        authorized_candidate_call_cap=2, planned_calls_after_strict_pruning=sum(r["decision"] == "evaluate" for r in rows),
        timeout_seconds=900, single_worker=True, original_saved_widths=[1], regenerate=False,
        baseline_new_calls=0, baseline_cost_not_counted_as_fair_comparison=True,
        early_stop="only necessary DDR bound strictly above independently verified baseline may cancel a preregistered candidate",
        acceptance="official success + complete frozen evidence verification + lexicographically lower (makespan,added_copy_bytes)",
        config_path=str(config), config_sha256=digest(config), source_sha256=frozen,
        read_only_evidence_sha256=files, candidates=rows, prepared_at=time.time())
    integrity(manifest)
    out.mkdir(parents=True)
    atomic_json(out / "manifest.json", manifest)
    atomic_json(out / "checkpoint.json", dict(state="prepared", completed=False, logical_calls=0,
        manifest_sha256=digest(out / "manifest.json"), results=[]))
    print("prepared", out, "calls", manifest["planned_calls_after_strict_pruning"], flush=True)
    return manifest


def execute(out):
    manifest = read_json(out / "manifest.json")
    checkpoint = read_json(out / "checkpoint.json")
    if checkpoint["state"] != "prepared" or checkpoint["logical_calls"] != 0:
        raise ValueError("execute once only; pending/finished work cannot be retried in place")
    manifest_hash = digest(out / "manifest.json")
    if checkpoint["manifest_sha256"] != manifest_hash:
        raise ValueError("prepared manifest changed")
    integrity(manifest)
    started, rows, calls = time.perf_counter(), [], 0
    def save(state, complete=False):
        d = dict(experiment=manifest["experiment"], state=state, completed=complete,
            manifest_sha256=manifest_hash, logical_calls=calls, authorized_candidate_call_cap=2,
            returned_calls=sum(r.get("state") == "returned" for r in rows),
            pending_calls=sum(r.get("state") == "pending" for r in rows),
            pruned_candidates=sum(r.get("state") == "pruned" for r in rows), results=rows,
            elapsed_seconds=time.perf_counter() - started)
        atomic_json(out / "checkpoint.json", d)
        return d
    for item in manifest["candidates"]:
        integrity(manifest)
        row = dict(case=item["case"], candidate_plan_sha256=item["candidate_plan_sha256"],
            baseline_objective=item["baseline_objective"], lower_bound=item["prune_lower_bound"],
            accepted=False, export_path=None)
        rows.append(row)
        if item["decision"] == "strict_ddr_bound_pruned":
            row.update(state="pruned", reason=item["prune_reason"], official_status="not_evaluated")
            save("running")
            continue
        if calls >= manifest["authorized_candidate_call_cap"]:
            raise ValueError("logical call cap exhausted")
        calls += 1
        row.update(state="pending", logical_call=calls, timeout_seconds=900, requested_at=time.time())
        save("evaluating")
        print("pending official call", calls, "case", item["case"], flush=True)
        plan = read_json(item["candidate_path"])
        try:
            record = evaluate(item["graph_path"], plan, 1, out / "evaluations", timeout=900,
                              config_path=manifest["config_path"])
        except Exception as exc:
            record = dict(status="wrapper_exception", error=repr(exc), traceback=traceback.format_exc(),
                          metrics={}, returncode=None, cache_hit=False)
        row.update(state="returned", record=record, official_status=record["status"], returned_at=time.time())
        save("verifying")
        try:
            integrity(manifest)
            if digest(out / "manifest.json") != manifest_hash:
                raise ValueError("manifest changed during evaluation")
            if record["status"] == "success":
                controller.Solver.verify_success(view(item, manifest), record, plan, item["candidate_plan_sha256"])
                row["verified_success"] = True
                row["objective"] = list(controller.objective(record))
                row["accepted"] = tuple(row["objective"]) < tuple(item["baseline_objective"])
                if row["accepted"]:
                    path = out / ("case_" + item["case"] + ".improved.plan.json")
                    atomic_json(path, plan)
                    row.update(export_path=str(path), export_file_sha256=digest(path))
        except Exception as exc:
            row.update(verified_success=False, accepted=False, evidence_error=repr(exc))
        save("running")
        print("returned", item["case"], row["official_status"], row.get("objective"),
              "accepted", row["accepted"], flush=True)
    integrity(manifest)
    result = save("finished", True)
    result["source_and_evidence_hashes_verified"] = True
    result["requires_review"] = any("evidence_error" in r for r in rows)
    result["baseline_retained_cases"] = [r["case"] for r in rows if not r["accepted"]]
    atomic_json(out / "summary.json", result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--execute-prepared", action="store_true")
    args = parser.parse_args()
    out = args.run_dir.expanduser().resolve()
    controller.refinement_helpers.outside_official(out)
    execute(out) if args.execute_prepared else prepare(out)
