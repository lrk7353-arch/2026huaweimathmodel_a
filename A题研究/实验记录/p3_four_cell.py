"""Frozen-selection four-cell measurements and a separate two-control ablation."""
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
ROOT = RESEARCH.parent
RUNS = RESEARCH / "advanced_solver/runs"
OUT = HERE / "P3四格_v2"
sys.path[:0] = [str(RESEARCH / "advanced_solver"), str(RESEARCH / "solver")]
from cache_refine import generate_cache_candidates
from common import object_digest
from evaluator import evaluate
from graph_ir import GraphIR
from plan import validate_plan

CASES = ("011", "019", "044", "049", "051", "064", "069", "071", "082", "093")
CONFIG = ROOT / "选题分析/A题附件/data/config.txt"
OFFICIAL = CONFIG.parent.parent / "code"


def read(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def raw(record):
    with gzip.open(record["result_path"], "rt", encoding="utf-8") as handle:
        return json.load(handle)


def key(record):
    return record["metrics"]["makespan"], record["metrics"].get("data_movement_bytes", {}).get("added_copy_bytes", 0)


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


def check_source(record):
    assert record["status"] == "success" and record["metrics"]["num_cores"] == 5
    assert digest(record["result_path"]) == record["result_sha256"]
    assert digest(record["graph_path"]) == record["hashes"]["graph_sha256"]
    assert digest(CONFIG) == record["hashes"]["config_sha256"]
    assert record["hashes"]["official_py_sha256"] == {p.name: digest(p) for p in sorted(OFFICIAL.glob("*.py"))}
    plan = read(record["plan_path"])
    assert object_digest(plan) == record["hashes"]["plan_sha256"]
    validate_plan(GraphIR.from_path(record["graph_path"]), plan)
    observation = raw(record)
    assert observation["makespan"] == record["metrics"]["makespan"]
    assert observation["data_movement_bytes"] == record["metrics"]["data_movement_bytes"]
    return plan


def manifest_hashes():
    paths = list(OFFICIAL.glob("*.py")) + list((RESEARCH / "solver").glob("*.py"))
    paths += list((RESEARCH / "advanced_solver").glob("*.py")) + [CONFIG, Path(__file__).resolve()]
    paths += [CONFIG.parent / ("case_" + case + ".json") for case in CASES]
    return {str(p): digest(p) for p in sorted(paths)}


def freeze_selection():
    progress = read(RUNS / "operation_all100_n5_v1/progress.json")
    assert len(progress["completed"]) == 100 and not progress["failures"]
    trace_path = RUNS / "trace_refine_v1/summary.json"
    trace = read(trace_path)
    traces = {r["case"]: r for r in trace["cases"]}
    pool, inputs, selected = list(records_in(trace)), [], []
    inputs.append({"path": str(trace_path), "sha256": digest(trace_path)})
    independent_path = HERE / "第二轮独立核验/verification.json"
    independent = read(independent_path)
    pool.extend(records_in(independent))
    inputs.append({"path": str(independent_path), "sha256": digest(independent_path), "purpose": "cache availability only, does not change plan selection"})
    for case in CASES:
        name = "case_" + case
        source_path = RUNS / "operation_all100_n5_v1/results" / (name + ".json")
        operation = read(source_path)
        inputs.append({"path": str(source_path), "sha256": digest(source_path)})
        pool.extend(records_in(operation))
        options = [{"family": "operation_selected", "source_path": str(source_path), "record": operation["selected"]["record"]}]
        if name in traces:
            options.append({"family": "trace_selected", "source_path": str(trace_path), "record": traces[name]["selected"]["record"]})
        cache_path = RUNS / "cache_refine_v1" / name / "summary.json"
        cache = read(cache_path) if cache_path.exists() else None
        if cache:
            inputs.append({"path": str(cache_path), "sha256": digest(cache_path)})
            pool.extend(records_in(cache))
            if cache["selected_p2_record"]["status"] == "success":
                options.append({"family": "cache_selected_exact_p2_pair", "source_path": str(cache_path), "record": cache["selected_p2_record"]})
        pi2 = min(options, key=lambda option: key(option["record"]))  # first source wins equal metric ties
        plan2 = check_source(pi2["record"])
        if cache and cache["strictly_improved"]:
            pi3 = {"family": "accepted_cache_selected", "source_path": str(cache_path), "record": cache["selected_record"]}
            plan3 = check_source(pi3["record"])
            policy = "previously_accepted_P3_plan_no_reselection"
        else:
            pi3, plan3 = dict(pi2), plan2
            policy = "sameplan(no-policy-search-on-selected-pi2)"
        selection = {"case": name, "graph_path": pi2["record"]["graph_path"],
                     "pi2": pi2, "pi3": pi3, "pi2_plan": plan2, "pi3_plan": plan3,
                     "pi2_plan_sha256": object_digest(plan2), "pi3_plan_sha256": object_digest(plan3),
                     "policy_status": policy, "historical_cache_search_exists": cache is not None,
                     "historical_cache_search_strictly_improved": bool(cache and cache["strictly_improved"]),
                     "selection_options": [{"family": x["family"], "makespan": key(x["record"])[0],
                                            "plan_sha256": x["record"]["hashes"]["plan_sha256"]} for x in options]}
        save(OUT / name / "pi2.plan.json", plan2)
        save(OUT / name / "pi3.plan.json", plan3)
        selected.append(selection)
    old_path = HERE / "independent_local_move_check/verification.json"
    old = read(old_path)["evaluations"]["3"]
    assert old["metrics"]["makespan"] == 8190
    check_source(old)
    pool.append(old)
    inputs.append({"path": str(old_path), "sha256": digest(old_path), "purpose": "separate071controlstart"})
    protocol = {"created_utc": datetime.now(timezone.utc).isoformat(), "cases": list(CASES), "num_cores": 5,
                "workers": 1, "timeout_seconds": 120, "max_four_cell_logical_calls": 40,
                "max_control_p3_logical_calls": 3, "frozen_before_new_evaluation": True,
                "selection_rule": "best current selected P2; only accepted strict P3 improvement used as pi3; otherwise pi3=pi2; no feedback/reselection",
                "tie_rule": "lower makespan, then added_copy_bytes, then source order operation/trace/cache",
                "control_rule": "base replay then two unconditional observed-order reencodings; also derive strict-incumbent retention from exact repeated plan hashes",
                "input_sources": inputs, "source_hashes": manifest_hashes(), "selection": selected,
                "control071_start": old, "cache_records_available": len(pool)}
    save(OUT / "selection_manifest.json", protocol)
    return protocol, pool


def seed_valid_cache(pool):
    seeded = []
    seen = set()
    for record in pool:
        cache_key = record.get("cache_key")
        if not cache_key or cache_key in seen or record.get("status") != "success":
            continue
        seen.add(cache_key)
        if digest(record["result_path"]) != record["result_sha256"]:
            raise ValueError("changed cached official result")
        save(OUT / "evaluations/cache" / cache_key / "success.json", record)
        seeded.append({"cache_key": cache_key, "record_path": record["record_path"],
                       "problem": record["problem"], "plan_sha256": record["hashes"]["plan_sha256"],
                       "python": record["hashes"].get("python")})
    save(OUT / "cache_seed_manifest.json", seeded)


def evaluate_cell(graph, plan, problem):
    record = evaluate(graph, plan, problem, OUT / "evaluations", timeout=120.0, config_path=CONFIG, official_code=OFFICIAL)
    assert record["hashes"]["plan_sha256"] == object_digest(plan)
    if record["status"] == "success":
        observation = raw(record)
        assert observation["makespan"] == record["metrics"]["makespan"]
        assert observation["data_movement_bytes"] == record["metrics"]["data_movement_bytes"]
    return record


def main():
    assert not (OUT / "selection_manifest.json").exists(), "use a new versioned directory instead of mutating a completed protocol"
    started = time.monotonic()
    protocol, pool = freeze_selection()
    seed_valid_cache(pool)
    cases, calls = [], []
    for item in protocol["selection"]:
        cells = {}
        for label, problem, plan_name in (("t2_pi2", 2, "pi2_plan"), ("t3_pi2", 3, "pi2_plan"),
                                           ("t2_pi3", 2, "pi3_plan"), ("t3_pi3", 3, "pi3_plan")):
            assert len(calls) < 40
            record = evaluate_cell(item["graph_path"], item[plan_name], problem)
            calls.append({"case": item["case"], "cell": label, "record": record})
            cells[label] = record
            save(OUT / "four_cell_calls.json", calls)
        ratios = None
        if all(record["status"] == "success" for record in cells.values()):
            t2a, t3a, t3b = (cells[label]["metrics"]["makespan"] for label in ("t2_pi2", "t3_pi2", "t3_pi3"))
            ratios = {"hardware_t2pi2_over_t3pi2": t2a / t3a, "policy_t3pi2_over_t3pi3": t3a / t3b,
                      "total_t2pi2_over_t3pi3": t2a / t3b, "factorization_residual": (t2a / t3a) * (t3a / t3b) - t2a / t3b}
            assert abs(ratios["factorization_residual"]) < 1e-12
        case = {"case": item["case"], "pi2_plan_sha256": item["pi2_plan_sha256"], "pi3_plan_sha256": item["pi3_plan_sha256"],
                "policy_status": item["policy_status"], "historical_cache_search_exists": item["historical_cache_search_exists"],
                "cells": cells, "ratios": ratios,
                "static_copy_bytes": {k: r["metrics"].get("data_movement_bytes") for k, r in cells.items()},
                "dynamic_cache_stats": {k: r["metrics"].get("cache_stats") for k, r in cells.items() if r["problem"] == 3}}
        save(OUT / item["case"] / "four_cells.json", case)
        cases.append(case)
        print(item["case"], [cells[k]["metrics"].get("makespan") for k in cells], item["policy_status"], flush=True)
    # Separate ablation: never changes any four-cell plan or feeds selection back.
    old = protocol["control071_start"]
    base_plan = read(old["plan_path"])
    base = evaluate_cell(old["graph_path"], base_plan, 3)
    ablation_calls = [{"step": "base", "record": base, "plan": base_plan}]
    ir = GraphIR.from_path(old["graph_path"])
    control_plan, current = base_plan, base
    generations = []
    for round_index in range(2):
        if current["status"] != "success":
            break
        candidates, diagnostics = generate_cache_candidates(ir, control_plan, raw(current), num_cores=5,
                                                            max_candidates=1, round_index=round_index, seed=0)
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate["name"] == "cache_reencode_control" and candidate["metadata"]["is_reencoding_control"]
        assert candidate["metadata"]["changed_core_count"] == 0 and candidate["metadata"]["target"] is None
        generations.append({"round": round_index, "candidate": candidate, "diagnostics": diagnostics})
        control_plan = candidate["plan"]
        current = evaluate_cell(old["graph_path"], control_plan, 3)
        ablation_calls.append({"step": "unconditional_control_round_" + str(round_index), "record": current, "plan": control_plan})
    # With strict acceptance, determine the next control from the retained input.
    successful = [r for r in ablation_calls[:2] if r["record"]["status"] == "success"]
    retained = min(successful, key=lambda r: r["record"]["metrics"]["makespan"])
    strict_next, _ = generate_cache_candidates(ir, retained["plan"], raw(retained["record"]), num_cores=5,
                                                max_candidates=1, round_index=1, seed=0)
    matched = next((r for r in ablation_calls if r["record"]["hashes"]["plan_sha256"] == object_digest(strict_next[0]["plan"])), None)
    strict_final = min([retained] + ([matched] if matched and matched["record"]["status"] == "success" else []),
                       key=lambda r: r["record"]["metrics"]["makespan"])
    ablation = {"scope": "separate pure-observed-order-control ablation, no directed targeting, no reassignment",
                "calls": ablation_calls, "generation": generations, "logical_call_count": len(ablation_calls),
                "strict_round1_retained_plan_sha256": retained["record"]["hashes"]["plan_sha256"],
                "strict_round2_control_plan_sha256": object_digest(strict_next[0]["plan"]),
                "strict_round2_existing_exact_plan_record": matched["record"] if matched else None,
                "strict_final_makespan": strict_final["record"]["metrics"]["makespan"] if matched else None,
                "full_cache_probe_final_makespan_for_context": 7952,
                "strict_result_derived_from_existing_exact_plan_not_an_extra_evaluation": matched is not None}
    save(OUT / "control071_ablation.json", ablation)
    all_records = [r["record"] for r in calls + ablation_calls]
    result = {"selection_manifest": str(OUT / "selection_manifest.json"), "cases": cases,
              "four_cell_logical_calls": len(calls), "control_logical_calls": len(ablation_calls),
              "cache_hits": sum(bool(r["cache_hit"]) for r in all_records),
              "fresh_worker_calls": sum(not r["cache_hit"] for r in all_records),
              "status_counts": dict(Counter(r["status"] for r in all_records)),
              "source_hashes_unchanged": protocol["source_hashes"] == manifest_hashes(),
              "source_hashes_after": manifest_hashes(), "no_selection_feedback": True,
              "control_ablation_path": str(OUT / "control071_ablation.json"),
              "elapsed_seconds": time.monotonic() - started,
              "scope": "10 preselected mechanism cases, not an all100 aggregate or unbiased generalization estimate"}
    save(OUT / "summary.json", result)
    print("DONE", result["four_cell_logical_calls"], result["control_logical_calls"], result["cache_hits"], result["fresh_worker_calls"],
          "control sequence", [r["record"]["metrics"]["makespan"] for r in ablation_calls], "strict", ablation["strict_final_makespan"], flush=True)


if __name__ == "__main__":
    main()
