"""Frozen best-known P3 refinement; independent of the fair-v2 solver campaign."""
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time

OUT = Path(__file__).resolve().parent
EXPERIMENTS = OUT.parent
RESEARCH = EXPERIMENTS.parent
ROOT = RESEARCH.parent
ADVANCED = RESEARCH / "advanced_solver"
RUNS = ADVANCED / "runs"
DATA = ROOT / "选题分析/A题附件/data"
OFFICIAL = DATA.parent / "code"
sys.path[:0] = [str(ADVANCED), str(RESEARCH / "solver")]
from cache_refine import generate_cache_candidates
from common import object_digest
from evaluator import evaluate
from graph_ir import GraphIR
from plan import validate_plan

CASES = ("011", "019", "044", "049", "051", "064", "069", "071", "082", "093")
CAP, ROUNDS, WIDTH, SEED = 32, 3, 12, 0


def read(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def raw(record):
    with gzip.open(record["result_path"], "rt", encoding="utf-8") as handle:
        return json.load(handle)


def objective(record):
    return (record["metrics"]["makespan"], record["metrics"]["data_movement_bytes"]["added_copy_bytes"])


def records_in(value):
    if isinstance(value, dict):
        if {"status", "metrics", "hashes", "problem", "result_path", "plan_path", "graph_path"} <= set(value):
            if value["status"] == "success":
                yield value
            return
        for item in value.values():
            yield from records_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from records_in(item)


def fixed_hashes():
    files = list(ADVANCED.glob("*.py")) + list((RESEARCH / "solver").glob("*.py")) + list(OFFICIAL.glob("*.py"))
    files += [DATA / "config.txt", Path(__file__).resolve()] + [DATA / ("case_" + case + ".json") for case in CASES]
    return {str(path): sha(path) for path in sorted(files)}


def verify(record):
    assert record["status"] == "success" and record["metrics"]["num_cores"] == 5
    assert sha(record["result_path"]) == record["result_sha256"]
    assert sha(record["graph_path"]) == record["hashes"]["graph_sha256"]
    assert sha(DATA / "config.txt") == record["hashes"]["config_sha256"]
    assert record["hashes"]["official_py_sha256"] == {p.name: sha(p) for p in sorted(OFFICIAL.glob("*.py"))}
    plan = read(record["plan_path"])
    assert object_digest(plan) == record["hashes"]["plan_sha256"]
    validate_plan(GraphIR.from_path(record["graph_path"]), plan)
    result = raw(record)
    assert result["makespan"] == record["metrics"]["makespan"]
    assert result["data_movement_bytes"] == record["metrics"]["data_movement_bytes"]
    return plan


def freeze():
    snapshots, pool, items = [], [], []

    def frozen_read(path):
        content = Path(path).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        snapshot = OUT / "frozen_inputs" / ("{:03d}_".format(len(snapshots)) + Path(path).name)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(content)
        snapshots.append({"source_path": str(path), "sha256": digest, "snapshot_path": str(snapshot)})
        return json.loads(content)

    progress = frozen_read(RUNS / "operation_all100_n5_v1/progress.json")
    assert len(progress["completed"]) == 100 and not progress["failures"]
    trace_path = RUNS / "trace_refine_v1/summary.json"
    trace = frozen_read(trace_path)
    assert (RUNS / "trace_refine_v1/completion.json").exists()
    trace_cases = {r["case"]: r for r in trace["cases"]}
    beam_path = EXPERIMENTS / "beam_refinement/run_v1/summary.json"
    beam = frozen_read(beam_path) if beam_path.exists() else {"cases": []}
    beam_cases = {r["case"]: r for r in beam["cases"] if r.get("strategy_stats") and
                  all(s.get("stop_reason") for s in r["strategy_stats"].values())}
    four_path = EXPERIMENTS / "P3四格_v2/summary.json"
    four = frozen_read(four_path)
    assert four["status_counts"] == {"success": 43} and four["no_selection_feedback"]
    four_cases = {r["case"]: r for r in four["cases"]}
    for value in (trace, beam, four):
        pool.extend(records_in(value))
    for case in CASES:
        name = "case_" + case
        path = RUNS / "operation_all100_n5_v1/results" / (name + ".json")
        operation = frozen_read(path)
        pool.extend(records_in(operation))
        p2 = [{"family": "operation_selected", "source": str(path), "record": operation["selected"]["record"]}]
        if name in trace_cases:
            p2.append({"family": "trace_selected", "source": str(trace_path), "record": trace_cases[name]["selected"]["record"]})
        if name in beam_cases:
            p2.append({"family": "completed_beam_case_selected", "source": str(beam_path), "record": beam_cases[name]["selected"]["record"]})
        p3 = [{"family": "completed_four_cell_" + label, "source": str(four_path), "record": four_cases[name]["cells"][label]}
              for label in ("t3_pi2", "t3_pi3")]
        cache_path = RUNS / "cache_refine_v1" / name / "summary.json"
        if cache_path.exists():
            cache = frozen_read(cache_path)
            pool.extend(records_in(cache))
            p2.append({"family": "old_cache_selected_p2_pair", "source": str(cache_path), "record": cache["selected_p2_record"]})
            p3.append({"family": "old_cache_selected", "source": str(cache_path), "record": cache["selected_record"]})
        for option in p2 + p3:
            option["plan"] = verify(option["record"])
            option["plan_sha256"] = object_digest(option["plan"])
        best_p2 = min(p2, key=lambda x: objective(x["record"]))
        best_p3 = min(p3, key=lambda x: objective(x["record"]))
        items.append({"case": name, "graph_path": str(DATA / (name + ".json")), "p2_options": p2, "p3_options": p3,
                      "frozen_p2": best_p2, "frozen_existing_p3": best_p3})
        save(OUT / name / "frozen_p2.plan.json", best_p2["plan"])
        save(OUT / name / "frozen_existing_p3.plan.json", best_p3["plan"])
    protocol = {"created_utc": datetime.now(timezone.utc).isoformat(), "scope": "best-known P3 deep exploration; not fair-v2; no formal result feed-in",
                "cases": list(CASES), "num_cores": 5, "workers": 1, "timeout_seconds": 60,
                "logical_cap_per_case_all_scenes": CAP, "total_logical_cap": CAP * len(CASES),
                "max_rounds": ROUNDS, "max_candidates_per_round": WIDTH, "seed": SEED,
                "initialization": "P3 evaluate frozen best P2 plan and frozen existing best P3 plan, exact duplicates share one charged evaluation",
                "acceptance": "lexicographic (makespan, added_copy_bytes); only official success",
                "stop_rule": "up to3 preregistered windows even after a nonimproving round; stop at budget or generation failure; never add cases or source results mid-run",
                "dedup": "same exact serialized plan within case/scene is not called again; failed attempts remain in seen set",
                "reserve": "reserve1 of32 logical calls for final P2 of selected P3 plan; reference frozen exact P2 record if already known",
                "control_comparison": "round0 observed-order control is the one-round control-only arm from the same initialized P3 incumbent",
                "cache_policy": "verified existing cache indices may be reused; every evaluate call including cache hit consumes budget",
                "frozen_completed_beam_cases": sorted(beam_cases), "source_snapshots": snapshots,
                "fixed_hashes": fixed_hashes(), "selection": items}
    save(OUT / "manifest.json", protocol)
    seeds, seen = [], set()
    for record in pool:
        key = record.get("cache_key")
        if not key or key in seen:
            continue
        seen.add(key)
        assert sha(record["result_path"]) == record["result_sha256"]
        save(OUT / "evaluations/cache" / key / "success.json", record)
        seeds.append({"cache_key": key, "record_path": record["record_path"], "result_sha256": record["result_sha256"]})
    save(OUT / "cache_seed_manifest.json", seeds)
    return protocol


def run_case(item):
    case, graph = item["case"], item["graph_path"]
    directory = OUT / case
    ir = GraphIR.from_path(graph)
    calls, reused, seen, generations, transitions = [], [], {}, [], []
    best = None

    def evaluate_plan(plan, problem, *, phase, name, metadata=None, parent_hash=None):
        hashed = object_digest(plan)
        lookup = problem, hashed
        if lookup in seen:
            original = seen[lookup]
            alias = {"problem": problem, "plan_sha256": hashed, "phase": phase, "name": name,
                     "original_logical_index": original["logical_index"], "reason": "exact internal duplicate; no new evaluate call"}
            reused.append(alias)
            return {**original, "phase": phase, "name": name, "metadata": metadata,
                    "parent_hash": parent_hash, "internal_duplicate": True}
        if len(calls) >= CAP:
            raise RuntimeError("hard per-case budget exhausted")
        validate_plan(ir, plan)
        record = evaluate(graph, plan, problem, OUT / "evaluations", timeout=60.0,
                          config_path=DATA / "config.txt", official_code=OFFICIAL)
        row = {"logical_index": len(calls) + 1, "phase": phase, "name": name, "problem": problem,
               "plan_sha256": hashed, "plan": plan, "metadata": metadata, "parent_hash": parent_hash,
               "internal_duplicate": False, "record": record}
        calls.append(row)
        seen[lookup] = row
        assert record["hashes"]["plan_sha256"] == hashed
        save(directory / "all_calls.json", calls)
        return row

    initial_rows = []
    for label, option in (("p2_replayed_in_p3", item["frozen_p2"]), ("existing_p3_rechecked", item["frozen_existing_p3"])):
        row = evaluate_plan(option["plan"], 3, phase="initialization", name=label,
                            metadata={"source": option["source"], "source_family": option["family"], "source_record": option["record"]})
        initial_rows.append(row)
        if row["record"]["status"] == "success" and (best is None or objective(row["record"]) < objective(best["record"])):
            best = row
    if best is None:
        result = {"case": case, "status": "no_valid_initialization", "initialization": initial_rows,
                  "logical_calls": len(calls), "calls": calls, "failures_retained": True}
        save(directory / "summary.json", result)
        return result
    initialized = best
    control_only = None
    generation_errors = []
    round_results = []
    for round_index in range(ROUNDS):
        remaining = CAP - 1 - len(calls)
        if remaining <= 0:
            break
        parent = best
        try:
            candidates, diagnostics = generate_cache_candidates(ir, parent["plan"], raw(parent["record"]), num_cores=5,
                                                                max_candidates=WIDTH, round_index=round_index, seed=SEED)
        except Exception as error:
            generation_errors.append({"round": round_index, "error": repr(error), "input_plan_hash": parent["plan_sha256"]})
            break
        assert candidates and candidates[0]["name"] == "cache_reencode_control"
        assert candidates[0]["metadata"]["is_reencoding_control"]
        generation = {"round": round_index, "input_plan_sha256": parent["plan_sha256"],
                      "input_record": parent["record"], "candidates": candidates, "diagnostics": diagnostics}
        generations.append(generation)
        save(directory / ("round_{:02d}_generation.json".format(round_index)), generation)
        round_calls, improvements = 0, []
        for candidate in candidates:
            if len(calls) >= CAP - 1:
                break
            is_control = candidate["metadata"]["is_reencoding_control"]
            phase = "reencoding_control" if is_control else "directed_cache"
            before_count = len(calls)
            row = evaluate_plan(candidate["plan"], 3, phase=phase, name=candidate["name"],
                                metadata={**candidate["metadata"], "round": round_index}, parent_hash=parent["plan_sha256"])
            round_calls += len(calls) - before_count
            if round_index == 0 and is_control:
                control_only = row
            if row["record"]["status"] == "success" and objective(row["record"]) < objective(best["record"]):
                transition = {"round": round_index, "phase": phase, "name": row["name"],
                              "from_plan_sha256": best["plan_sha256"], "to_plan_sha256": row["plan_sha256"],
                              "from_objective": list(objective(best["record"])), "to_objective": list(objective(row["record"])),
                              "proposal_generated_from": parent["plan_sha256"], "logical_index": row["logical_index"]}
                transitions.append(transition); improvements.append(transition)
                best = row
        round_results.append({"round": round_index, "generated": len(candidates), "new_logical_calls": round_calls,
                              "input_objective": list(objective(parent["record"])), "retained_objective": list(objective(best["record"])),
                              "improvements": improvements})
        save(directory / "progress.json", {"logical_calls": len(calls), "rounds": round_results,
                                            "best": best, "transitions": transitions})
    assert control_only is not None or generation_errors
    # One reserved budget slot makes the final exact-plan four-cell complete.
    known_p2 = next((option["record"] for option in item["p2_options"] if option["plan_sha256"] == best["plan_sha256"]), None)
    if known_p2 is None:
        final_p2 = evaluate_plan(best["plan"], 2, phase="final_four_cell_p2", name="exact_selected_plan_p2")
        selected_p2_record = final_p2["record"]
    else:
        selected_p2_record = known_p2
    p2_in_p3 = initial_rows[0]["record"]
    cells = {"t2_pi2": item["frozen_p2"]["record"], "t3_pi2": p2_in_p3,
             "t2_pi3": selected_p2_record, "t3_pi3": best["record"]}
    ratios = None
    if all(record["status"] == "success" for record in cells.values()):
        t2a, t3a, t3b = (cells[k]["metrics"]["makespan"] for k in ("t2_pi2", "t3_pi2", "t3_pi3"))
        ratios = {"hardware": t2a / t3a, "policy": t3a / t3b, "total": t2a / t3b}
    successful_controls = [initialized] + ([control_only] if control_only and control_only["record"]["status"] == "success" else [])
    control_retained = min(successful_controls, key=lambda row: objective(row["record"]))
    attribution = {family: sum(t["from_objective"][0] - t["to_objective"][0] for t in transitions if t["phase"] == family)
                   for family in ("reencoding_control", "directed_cache")}
    result = {"case": case, "status": "success", "scope": "best-known inherited-start deep search; not fair-v2",
              "initialization": initial_rows, "initialized": initialized, "control_only_round0": control_only,
              "control_only_retained": control_retained, "selected": best,
              "strict_time_improved": objective(best["record"])[0] < objective(initialized["record"])[0],
              "objective_improved": objective(best["record"]) < objective(initialized["record"]),
              "time_saved_from_initialized": objective(initialized["record"])[0] - objective(best["record"])[0],
              "time_saved_beyond_round0_control": objective(control_retained["record"])[0] - objective(best["record"])[0],
              "accepted_path_time_decreases_by_family_not_causal_effects": attribution,
              "selected_final_family": best["phase"], "transitions": transitions, "rounds": round_results,
              "logical_calls": len(calls), "logical_calls_by_phase": dict(Counter(r["phase"] for r in calls)),
              "status_counts": dict(Counter(r["record"]["status"] for r in calls)),
              "cache_hits": sum(bool(r["record"]["cache_hit"]) for r in calls),
              "new_worker_calls": sum(not r["record"]["cache_hit"] for r in calls),
              "internal_duplicates": reused, "generation_errors": generation_errors,
              "four_cells": cells, "four_cell_ratios": ratios,
              "four_cell_hashes": {k: r["hashes"]["plan_sha256"] for k, r in cells.items()},
              "all_calls_path": str(directory / "all_calls.json"),
              "no_results_added_to_fair_campaign": True}
    assert cells["t2_pi2"]["hashes"]["plan_sha256"] == cells["t3_pi2"]["hashes"]["plan_sha256"]
    assert cells["t2_pi3"]["hashes"]["plan_sha256"] == cells["t3_pi3"]["hashes"]["plan_sha256"]
    assert len(calls) <= CAP
    save(directory / "selected.plan.json", best["plan"])
    save(directory / "summary.json", result)
    return result


def main():
    assert not (OUT / "manifest.json").exists(), "freeze once; do not replace an existing experiment"
    started = time.monotonic()
    protocol = freeze()
    summaries = []
    for item in protocol["selection"]:
        result = run_case(item)
        summaries.append(result)
        save(OUT / "progress.json", {"completed": len(summaries), "expected": len(CASES), "cases": summaries})
        if result["status"] == "success":
            print(item["case"], "init", objective(result["initialized"]["record"]),
                  "control1", objective(result["control_only_retained"]["record"]),
                  "best", objective(result["selected"]["record"]), "calls", result["logical_calls"], flush=True)
    counts = Counter()
    for case in summaries:
        counts.update(case.get("status_counts", {}))
    summary = {"scope": protocol["scope"], "cases": summaries, "logical_calls": sum(r["logical_calls"] for r in summaries),
               "status_counts": dict(counts), "cache_hits": sum(r.get("cache_hits", 0) for r in summaries),
               "new_worker_calls": sum(r.get("new_worker_calls", 0) for r in summaries),
               "strict_time_improved_cases": [r["case"] for r in summaries if r.get("strict_time_improved")],
               "objective_improved_cases": [r["case"] for r in summaries if r.get("objective_improved")],
               "source_hashes_unchanged": protocol["fixed_hashes"] == fixed_hashes(), "fixed_hashes_after": fixed_hashes(),
               "elapsed_seconds": time.monotonic() - started, "no_dynamic_source_feed": True}
    assert summary["logical_calls"] <= CAP * len(CASES)
    save(OUT / "summary.json", summary)
    print("DONE", summary["logical_calls"], summary["status_counts"], summary["strict_time_improved_cases"],
          "hashes_unchanged", summary["source_hashes_unchanged"], flush=True)


if __name__ == "__main__":
    main()
