#!/usr/bin/env python3
"""Frozen-protocol beam exploration, separate from formal/fair-v2 campaigns.

Imports existing generators/evaluator read-only. Per graph: greedy <=30 logical
trials then beam <=66, at most five layers and six candidates/parent. One worker.
The common-prefix comparison is descriptive matched logical-budget evidence;
the larger beam budget by itself is never evidence of algorithm superiority.
"""
import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
import shutil
import sys
import time

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parents[1]
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH / "solver"))
from solver.common import DATA, OFFICIAL, atomic_json, digest, object_digest, read_json
from solver.graph_ir import GraphIR
from solver.evaluator import evaluate
from advanced_solver.engine import source_hashes
from advanced_solver.trace_refine import generate_trace_candidates


def score(record):
    m = record["metrics"]
    return m["makespan"], m.get("data_movement_bytes", {}).get("added_copy_bytes", 0)


def make_state(plan, record, *, parent=None, layer=-1, candidate="initial", global_before=None):
    return {"key": object_digest(plan), "plan": plan, "record": record, "parent": parent,
            "layer": layer, "candidate": candidate, "metrics": list(score(record)),
            "global_before": list(global_before) if global_before is not None else None}


def _features(ir, plan):
    core_by_sg, rank_by_sg = {}, {}
    for c, schedule in enumerate(plan["core_schedules"]):
        for rank, sg in enumerate(schedule):
            core_by_sg[sg] = c
            rank_by_sg[sg] = rank / max(1, len(schedule) - 1)
    mapping = {int(op): sg for op, sg in plan["node_to_subgraph"].items()}
    global_rank = {op: i / max(1, len(mapping) - 1) for i, op in enumerate(mapping)}
    return [(core_by_sg[mapping[o]], rank_by_sg[mapping[o]], global_rank[o]) for o in ir.compute_ids]


def distance(a, b):
    if not a:
        return 0.0
    return sum(0.7 * (x[0] != y[0]) + 0.2 * abs(x[1] - y[1]) + 0.1 * abs(x[2] - y[2])
               for x, y in zip(a, b)) / len(a)


def select_frontier(ir, pool, best, width, slack):
    """Best first, then farthest-first among <=8%-slower successful states."""
    unique = {s["key"]: s for s in pool}
    unique[best["key"]] = best
    threshold = best["metrics"][0] * (1.0 + slack)
    eligible = {k: s for k, s in unique.items() if s["metrics"][0] <= threshold}
    selected = [best]
    features = {k: _features(ir, s["plan"]) for k, s in eligible.items()}
    decisions = [{"key": best["key"], "accepted": True, "reason": "global_incumbent", "distance": 0.0}]
    while len(selected) < width and len(selected) < len(eligible):
        alternatives = []
        for key, state in eligible.items():
            if any(s["key"] == key for s in selected):
                continue
            minimum = min(distance(features[key], features[s["key"]]) for s in selected)
            alternatives.append((-minimum, state["metrics"], key, state))
        negative_distance, _, key, state = min(alternatives, key=lambda row: row[:3])
        selected.append(state)
        decisions.append({"key": key, "accepted": True, "reason": "diverse_within_slack", "distance": -negative_distance})
    accepted = {s["key"] for s in selected}
    for key, state in unique.items():
        if key not in accepted:
            decisions.append({"key": key, "accepted": False,
                "reason": "outside_slack" if key not in eligible else "beam_width", "metrics": state["metrics"]})
    return selected, {"threshold_makespan": threshold, "global_best": best["metrics"], "decisions": decisions}


def search_strategy(ir, initial, *, width, logical_cap, protocol, generate, evaluate_one,
                    allowed=lambda: True, emit=lambda event: None):
    """Generic controller; callbacks make meaningful zero-evaluator tests possible."""
    best = initial
    frontier = [initial]
    nodes = {initial["key"]: initial}
    seen = {initial["key"]}
    trials, layers, duplicates, generation_failures = [], [], [], []
    best_by_prefix = [{"key": best["key"], "metrics": best["metrics"]}]
    stop_reason = "max_rounds"
    for layer in range(protocol["max_rounds"]):
        if len(trials) >= logical_cap:
            stop_reason = "logical_cap"
            break
        if not allowed():
            stop_reason = "evaluation_time_budget"
            break
        parents = list(frontier)
        successors = []
        layer_best_before = best["metrics"]
        expanded = []
        partial = False
        for parent in parents:
            if len(trials) >= logical_cap or not allowed():
                partial = True
                break
            parent_event = {"event": "expand", "layer": layer, "parent": parent["key"],
                            "parent_metrics": parent["metrics"], "global_before_expansion": best["metrics"],
                            "parent_is_global_incumbent": parent["key"] == best["key"]}
            emit(parent_event)
            try:
                candidates, diagnostics = generate(parent, layer, protocol["candidates_per_parent"])
            except Exception as error:
                failure = {**parent_event, "event": "generation_failure", "error": repr(error)}
                generation_failures.append(failure)
                emit(failure)
                continue
            expanded.append(parent["key"])
            emit({"event": "generation", "layer": layer, "parent": parent["key"],
                  "candidate_count": len(candidates), "diagnostics": diagnostics})
            for candidate in candidates:
                key = object_digest(candidate["plan"])
                if key in seen:
                    duplicate = {"event": "duplicate", "layer": layer, "parent": parent["key"],
                                 "key": key, "candidate": candidate["name"], "reason": "strategy_already_attempted"}
                    duplicates.append(duplicate)
                    emit(duplicate)
                    continue
                if len(trials) >= logical_cap or not allowed():
                    emit({"event": "not_evaluated", "layer": layer, "parent": parent["key"], "key": key,
                          "candidate": candidate["name"], "reason": "logical_cap" if len(trials) >= logical_cap else "evaluation_time_budget"})
                    partial = True
                    break
                seen.add(key)
                before = best["metrics"]
                record = evaluate_one(candidate)
                row = {"event": "trial", "logical_index": len(trials) + 1, "layer": layer,
                       "parent": parent["key"], "parent_metrics": parent["metrics"],
                       "key": key, "candidate": candidate["name"], "metadata": candidate.get("metadata", {}),
                       "record": record, "global_before": before}
                if record["status"] == "success":
                    state = make_state(candidate["plan"], record, parent=parent["key"], layer=layer,
                                       candidate=candidate["name"], global_before=before)
                    nodes[key] = state
                    successors.append(state)
                    if state["metrics"] < best["metrics"]:
                        best = state
                    row["worse_than_parent"] = state["metrics"] > parent["metrics"]
                    row["worse_than_incumbent_at_creation"] = state["metrics"] > before
                row["global_after"] = best["metrics"]
                trials.append(row)
                best_by_prefix.append({"key": best["key"], "metrics": best["metrics"]})
                emit({**row, "plan": candidate["plan"]})
            if partial:
                break
        frontier, decisions = select_frontier(ir, parents + successors, best, width, protocol["relative_makespan_slack"])
        layer_row = {"event": "layer_end", "layer": layer, "parents": [s["key"] for s in parents],
                     "expanded_parents": expanded, "next_frontier": [s["key"] for s in frontier],
                     "global_before": layer_best_before, "global_after": best["metrics"],
                     "logical_trials_so_far": len(trials), "partial_layer": partial, **decisions}
        layers.append(layer_row)
        emit(layer_row)
        if partial:
            stop_reason = "logical_cap" if len(trials) >= logical_cap else "evaluation_time_budget"
            break
    return {"width": width, "logical_cap": logical_cap, "logical_trials": len(trials), "best": best,
            "best_by_prefix": best_by_prefix, "nodes": nodes, "trials": trials, "layers": layers,
            "duplicates": duplicates, "generation_failures": generation_failures, "stop_reason": stop_reason,
            "status_counts": dict(Counter(t["record"]["status"] for t in trials)),
            "official_calls": sum(not t["record"].get("cache_hit", False) for t in trials),
            "cache_hits": sum(bool(t["record"].get("cache_hit", False)) for t in trials)}


def ancestry(result, key):
    path, seen = [], set()
    while key is not None:
        if key in seen or key not in result["nodes"]:
            raise ValueError("broken or cyclic parent provenance")
        seen.add(key)
        node = result["nodes"][key]
        path.append({k: v for k, v in node.items() if k not in ("plan", "record")})
        key = node["parent"]
    path.reverse()
    return {"path": path,
            "has_worsening_parent_child_edge": any(b["metrics"] > a["metrics"] for a, b in zip(path, path[1:])),
            "has_ancestor_worse_than_incumbent_at_creation": any(n["global_before"] is not None and n["metrics"] > n["global_before"] for n in path),
            "interpretation": "observed first-discovery/actually-expanded ancestry, not proof every possible greedy path must fail"}


def checked_official(ir, entry):
    record = entry["record"]
    hashes = record.get("hashes", {})
    if record.get("status") != "success" or record.get("problem") != 2:
        raise ValueError("source must be successful official P2")
    if hashes.get("graph_sha256") != digest(ir.path) or hashes.get("config_sha256") != digest(DATA / "config.txt"):
        raise ValueError("source graph/config mismatch")
    if hashes.get("official_py_sha256") != {p.name: digest(p) for p in sorted(OFFICIAL.glob("*.py"))}:
        raise ValueError("source official code mismatch")
    if digest(record["result_path"]) != record["result_sha256"] or digest(record["plan_path"]) != hashes["plan_sha256"]:
        raise ValueError("source plan/result changed")
    plan = read_json(record["plan_path"])
    if "plan" in entry and object_digest(plan) != object_digest(entry["plan"]):
        raise ValueError("source entry and official plan differ")
    with gzip.open(record["result_path"], "rt", encoding="utf-8") as stream:
        official = json.load(stream)
    if official["makespan"] != score(record)[0] or official["num_cores"] != 5:
        raise ValueError("source metrics/core count mismatch")
    return plan, official


def choose_initials(protocol):
    trace_path = RESEARCH / "advanced_solver/runs/trace_refine_v1/summary.json"
    trace = {c["case"]: c["selected"] for c in read_json(trace_path)["cases"]}
    result = []
    for case in protocol["cases"]:
        ir = GraphIR.from_path(DATA / (case + ".json"))
        operation_path = RESEARCH / "advanced_solver/runs/operation_all100_n5_v1/results" / (case + ".json")
        operation = read_json(operation_path)
        if operation.get("status") != "success":
            raise ValueError("operation campaign slot did not finish successfully")
        options = [(operation_path, operation["selected"])]
        if case in trace:
            options.append((trace_path, trace[case]))
        for _, entry in options:
            checked_official(ir, entry)
        path, selected = min(options, key=lambda item: score(item[1]["record"]))
        plan, _ = checked_official(ir, selected)
        initial = make_state(plan, selected["record"], candidate="frozen_initial")
        result.append((ir, initial, {"selected_source": str(path), "source_sha256": digest(path),
            "options": [{"source": str(p), "sha256": digest(p), "metrics": list(score(e["record"]))} for p, e in options],
            "plan_object_sha256": initial["key"], "plan_file_sha256": digest(selected["record"]["plan_path"]),
            "result_sha256": selected["record"]["result_sha256"]}))
    return result


def serializable_result(result):
    return {**result, "nodes": {k: {f: v for f, v in s.items() if f != "plan"} for k, s in result["nodes"].items()}}


def compare_results(initial, greedy, beam, protocol):
    common = min(protocol["matched_comparison_cap"], greedy["logical_trials"], beam["logical_trials"])
    g = greedy["best_by_prefix"][common] if common else None
    b = beam["best_by_prefix"][common] if common else None
    matched = None if not common else ("beam_better" if b["metrics"] < g["metrics"] else "greedy_better" if g["metrics"] < b["metrics"] else "tie")
    return {"initial_metrics": initial["metrics"], "common_logical_prefix": common,
            "greedy_actual_trials": greedy["logical_trials"], "beam_actual_trials": beam["logical_trials"],
            "matched_greedy": g, "matched_beam": b, "matched_result": matched,
            "beam_final_metrics": beam["best"]["metrics"], "greedy_final_metrics": greedy["best"]["metrics"],
            "beam_matched_ancestry": ancestry(beam, b["key"]) if b else None,
            "beam_final_ancestry": ancestry(beam, beam["best"]["key"]),
            "interpretation": "common prefix matches logical trials, not walltime/compute; final beam may spend more"}


def write_report(out, summaries):
    lines = ["# 有限beam邻域：独立探索", "",
        "P2/5核、固定六图；greedy最多30次、beam最多66次，最多5轮、每父6候选、beam宽3、慢解窗口8%。",
        "公平性描述仅限共同逻辑调用前缀m；两边实际消耗、缓存命中和停止原因分别保留。不同预算的beam最终成绩单列，不进入fairv2。", "",
        "|图|起点|公共m|greedy@ m|beam@ m|共同前缀结论|greedy总调用|beam总调用|beam最终|最终路径含变差边|",
        "|---|---:|---:|---:|---:|---|---:|---:|---:|---|"]
    for row in summaries:
        c = row["comparison"]
        lines.append("|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|".format(row["case"], c["initial_metrics"][0],
            c["common_logical_prefix"], c["matched_greedy"]["metrics"][0] if c["matched_greedy"] else "不可比较",
            c["matched_beam"]["metrics"][0] if c["matched_beam"] else "不可比较", c["matched_result"],
            c["greedy_actual_trials"], c["beam_actual_trials"], c["beam_final_metrics"][0],
            c["beam_final_ancestry"]["has_worsening_parent_child_edge"]))
    lines += ["", "非单调祖先证据来自首次真实评估与实际扩展父链，不事后选择重复候选的替代父链。",
              "祖先劣于当时incumbent，与父→子严格变差，是两个不同标签。即使两者成立，也不能证明其他未搜索的贪心路径必然失败。",
              "如果公共前缀beam没有领先，不能以更大最终预算的成绩声称beam结构更优。",
              "没有运行P3后续或扩大图集合。所有候选失败、缓存命中、重复来源、beam门槛和预算截断都有记录。"]
    (out / "简报.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=HERE / "run_v1")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    protocol_path = HERE / "protocol.json"
    protocol = read_json(protocol_path)
    out = args.run_dir.resolve()
    if out == DATA.parent.resolve() or DATA.parent.resolve() in out.parents or (out.exists() and any(out.iterdir())):
        parser.error("new/empty output directory outside official attachments required")
    out.mkdir(parents=True, exist_ok=True)
    datasets = choose_initials(protocol)
    sources = {**source_hashes(), str(Path(__file__).resolve()): digest(Path(__file__))}
    manifest = {"protocol": protocol, "protocol_sha256": digest(protocol_path), "source_sha256": sources,
                "config_sha256": digest(DATA / "config.txt"),
                "initials": [{"case": ir.path.stem, "graph_sha256": digest(ir.path), "metrics": initial["metrics"], **provenance}
                             for ir, initial, provenance in datasets], "scope": "exploration; never fairv2",
                "sequential_strategy_order": ["greedy", "beam"], "cache_scope": "shared exact-plan cache within this experiment; logically charged"}
    atomic_json(out / "manifest.json", manifest)
    for source, expected in sources.items():
        path = Path(source)
        target = out / "source_snapshot" / path.relative_to(RESEARCH.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        if digest(target) != expected:
            raise ValueError("source changed while freezing experiment")
    shutil.copyfile(protocol_path, out / "protocol.json")
    for ir, initial, provenance in datasets:
        atomic_json(out / ir.path.stem / "initial.json", {**initial, "provenance": provenance})
    if args.prepare_only:
        print(json.dumps({"manifest": str(out / "manifest.json"), "initials": [(ir.path.stem, s["metrics"]) for ir, s, _ in datasets]}))
        return 0
    summaries = []
    global_seconds = 0.0
    total_logical = total_official = 0
    for ir, initial, _ in datasets:
        case_seconds = 0.0
        strategies = {}
        for name, width, cap in (("greedy", 1, protocol["greedy_logical_cap"]), ("beam", protocol["beam_width"], protocol["beam_logical_cap"])):
            directory = out / ir.path.stem / name
            directory.mkdir(parents=True, exist_ok=True)
            event_path = directory / "events.jsonl"
            with event_path.open("w", encoding="utf-8") as event_file:
                def emit(event):
                    if "plan" in event:
                        atomic_json(directory / "plans" / (event["key"] + ".json"), event["plan"])
                    event_file.write(json.dumps({k: v for k, v in event.items() if k != "plan"}, ensure_ascii=False) + "\n")
                    event_file.flush()

                def allowed():
                    return case_seconds < protocol["per_case_evaluation_seconds"] - 1 and global_seconds < protocol["global_evaluation_seconds"] - 1

                def generate(parent, layer, maximum):
                    _, official = checked_official(ir, parent)
                    return generate_trace_candidates(ir, parent["plan"], official, num_cores=5,
                        max_candidates=maximum, round_index=layer, seed=protocol["seed"])

                def evaluate_one(candidate):
                    nonlocal case_seconds, global_seconds
                    remaining = min(protocol["per_evaluation_timeout_seconds"],
                                    protocol["per_case_evaluation_seconds"] - case_seconds - 0.5,
                                    protocol["global_evaluation_seconds"] - global_seconds - 0.5)
                    started = time.monotonic()
                    record = evaluate(ir.path, candidate["plan"], 2, out / "evaluations", timeout=max(.01, remaining), config_path=DATA / "config.txt")
                    elapsed = time.monotonic() - started
                    case_seconds += elapsed
                    global_seconds += elapsed
                    return record

                result = search_strategy(ir, initial, width=width, logical_cap=cap, protocol=protocol,
                    generate=generate, evaluate_one=evaluate_one, allowed=allowed, emit=emit)
            strategies[name] = result
            atomic_json(directory / "result.json", serializable_result(result))
            atomic_json(directory / "selected.plan.json", result["best"]["plan"])
            total_logical += result["logical_trials"]
            total_official += result["official_calls"]
            print(json.dumps({"case": ir.path.stem, "strategy": name, "logical_calls": result["logical_trials"],
                "official_calls": result["official_calls"], "best": result["best"]["metrics"], "stop_reason": result["stop_reason"]}), flush=True)
        comparison = compare_results(initial, strategies["greedy"], strategies["beam"], protocol)
        selected = min((r["best"] for r in strategies.values()), key=lambda s: s["metrics"])
        row = {"case": ir.path.stem, "comparison": comparison, "selected": selected,
               "case_evaluation_seconds": case_seconds,
               "strategy_stats": {name: {k: r[k] for k in ("logical_trials", "official_calls", "cache_hits", "status_counts", "stop_reason")}
                                  for name, r in strategies.items()}}
        summaries.append(row)
        atomic_json(out / ir.path.stem / "selected.plan.json", selected["plan"])
        atomic_json(out / "summary.json", {"scope": "exploration, not fairv2", "cases": summaries,
                     "logical_calls": total_logical, "official_calls": total_official, "evaluation_seconds": global_seconds})
        write_report(out, summaries)
    unchanged = all(digest(path) == expected for path, expected in sources.items()) and digest(protocol_path) == manifest["protocol_sha256"]
    atomic_json(out / "completion.json", {"completed": True, "source_hashes_unchanged": unchanged,
        "logical_calls": total_logical, "official_calls": total_official, "evaluation_seconds": global_seconds,
        "all_six_cases_reported": len(summaries) == len(protocol["cases"]), "p3_calls": 0})
    if total_logical > protocol["all_cases_total_logical_cap"] or not unchanged:
        raise RuntimeError("budget/source freeze invariant violated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
