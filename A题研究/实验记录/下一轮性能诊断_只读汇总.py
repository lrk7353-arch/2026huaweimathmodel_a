"""Read-only snapshot of completed/current formal records; never evaluates plans."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import csv
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
FORMAL = ROOT / "advanced_solver/runs/formal_v2"
OUT = ROOT / "实验记录/下一轮性能诊断.json"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compact(row):
    if not row:
        return None
    r = row["record"]
    return {"stage": row["stage"], "name": row["name"], "status": r["status"],
            "metrics": r.get("metrics"), "plan_sha256": row.get("plan_sha256"),
            "record_path": r.get("record_path"), "plan_path": r.get("plan_path"),
            "result_path": r.get("result_path"), "result_sha256": r.get("result_sha256"),
            "cache_hit": r.get("cache_hit"), "elapsed_seconds": r.get("elapsed_seconds"),
            "metadata": {k: v for k, v in row.get("metadata", {}).items()
                         if isinstance(v, (str, int, float, bool, type(None)))}}


def key(row):
    m = row["record"]["metrics"]
    return m["makespan"], m.get("data_movement_bytes", {}).get("added_copy_bytes", 0)


def take_snapshot():
    with (ROOT / "方案审阅/结构核验/a_graph_structure.csv").open(encoding="utf-8-sig") as f:
        structure = {r["case"]: r for r in csv.DictReader(f)}
    rows, failures, selected = [], [], {}
    for p in (1, 2, 3):
        batch = FORMAL / f"full_p{p}_seed17"
        for attempt in sorted((batch / "slots").glob("case_*/p*_n*/attempt_*")):
            source = attempt / "summary.json"
            if not source.exists():
                source = attempt / "checkpoint.json"
            if not source.exists():
                continue
            data = source.read_bytes()
            try:
                s = json.loads(data)
            except (ValueError, OSError) as error:
                failures.append({"path": str(source), "error": str(error)})
                continue
            identity = s["case"], p, s["num_cores"]
            if identity in selected:
                raise ValueError("Multiple attempts require explicit selection: " + str(identity))
            selected[identity] = source
            grouped = defaultdict(list)
            for ev in s["evaluations"]:
                if ev["record"]["status"] == "success":
                    grouped[ev["stage"]].append(ev)
            bests = {stage: min(values, key=key) for stage, values in grouped.items()}
            before_trace = [v for k, values in grouped.items() if k in ("component", "operation", "initial", "fallback") for v in values]
            before_cache = before_trace + grouped.get("trace", [])
            row = {"case": s["case"], "problem": p, "num_cores": s["num_cores"],
                   "complete": bool(s.get("completed", False)), "state": s["state"],
                   "source_path": str(source.resolve()), "source_sha256": hashlib.sha256(data).hexdigest(),
                   "lower_bound": s["lower_bound"], "structure": structure[s["case"]],
                   "selected": compact(s.get("best")), "stage_best": {k: compact(v) for k, v in bests.items()},
                   "before_trace": compact(min(before_trace, key=key)) if before_trace else None,
                   "before_cache": compact(min(before_cache, key=key)) if before_cache else None,
                   "evaluated_count": s["evaluated_count"], "stage_attempt_counts": dict(Counter(ev["stage"] for ev in s["evaluations"])),
                   "candidate_status_counts": s["status_counts"], "generation_failures": s["generation_failures"],
                   "stages": [{k: v for k, v in stage.items() if k != "diagnostics"} for stage in s["stages"]],
                   "candidates": [compact(ev) for ev in s["evaluations"]]}
            rows.append(row)
    summary = {}
    for p in (1, 2, 3):
        complete = [r for r in rows if r["problem"] == p and r["complete"]]
        counts = {"complete_slots": len(complete), "expected_slots": 400,
                  "incomplete_checkpoints": sum(r["problem"] == p and not r["complete"] for r in rows),
                  "cases": sorted({r["case"] for r in complete}),
                  "best_stage_counts": dict(Counter(r["selected"]["stage"] for r in complete if r["selected"])),
                  "candidate_status_counts": dict(sum((Counter(r["candidate_status_counts"]) for r in complete), Counter())),
                  "generation_failure_count": sum(len(r["generation_failures"]) for r in complete)}
        for stage in ("operation", "trace", "cache"):
            pairs = []
            for r in complete:
                base = r["stage_best"].get("component") if stage == "operation" else r["before_" + stage]
                new = r["stage_best"].get(stage)
                if base and new:
                    b, n = base["metrics"]["makespan"], new["metrics"]["makespan"]
                    pairs.append({"case": r["case"], "cores": r["num_cores"], "base": b, "new": n,
                                  "ratio_new_over_base": n / b, "name": new["name"]})
            counts[stage] = {"pairs": len(pairs), "wins": sum(x["new"] < x["base"] for x in pairs),
                             "ties": sum(x["new"] == x["base"] for x in pairs),
                             "losses": sum(x["new"] > x["base"] for x in pairs),
                             "best_pairs": sorted(pairs, key=lambda x: x["ratio_new_over_base"])[:8],
                             "worst_pairs": sorted(pairs, key=lambda x: -x["ratio_new_over_base"])[:8]}
        summary[str(p)] = counts
    out = {"snapshot_utc": datetime.now(timezone.utc).isoformat(),
           "scope": "read-only finite snapshot of running batches; complete slots only in comparisons; no evaluator calls",
           "summary": summary, "snapshot_read_errors": failures, "rows": rows}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps({"path": str(OUT), "snapshot_utc": out["snapshot_utc"], "summary": summary}, ensure_ascii=False, indent=2))


def union_length(intervals):
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(b, merged[-1][1])
        else:
            merged.append([a, b])
    return sum(b - a for a, b in merged)


def assignment(row):
    p = json.loads(Path(row["plan_path"]).read_text())
    sgcore = {sg: k for k, groups in enumerate(p["core_schedules"]) for sg in groups}
    return {op: sgcore[sg] for op, sg in p["node_to_subgraph"].items()}, len(sgcore)


def enrich_timelines():
    out = json.loads(OUT.read_text())
    targets = ["003", "005", "009", "002", "008", "006", "015", "010", "016", "017"]
    out["priority_case_ids"] = targets
    out["priority_scope"] = "Diagnostic shortlist within formal snapshot coverage, not all100 optimal ranking; no new search"
    requests = []
    for r in out["rows"]:
        c = r["case"].split("_")[-1]
        wanted = (c in targets and r["num_cores"] == 5 and r["complete"] and r["problem"] in (1, 2))
        wanted |= c == "016" and r["num_cores"] == 4 and r["problem"] == 1 and r["complete"]
        wanted |= (c, r["problem"], r["num_cores"]) in [("021", 2, 5), ("004", 2, 3), ("002", 3, 2)]
        if wanted:
            for stage, ev in r["stage_best"].items():
                requests.append((r, stage, ev))
    evidence = {}
    links = []
    for slot, stage, ev in requests:
        path = ev["result_path"]
        if path not in evidence:
            assert sha(path) == ev["result_sha256"], path
            with gzip.open(path, "rt", encoding="utf-8") as f:
                result = json.load(f)
            assert result["makespan"] == ev["metrics"]["makespan"]
            assert result["data_movement_bytes"] == ev["metrics"]["data_movement_bytes"]
            cores = []
            for core in result["per_core_timeline"]:
                ops = core["ops"]
                work = {q: sum(o["duration"] for o in ops if o["pipe"] == q) for q in ("PIPE_M", "PIPE_V", "PIPE_MTE2", "PIPE_MTE3")}
                compute = [(o["start"], o["end"]) for o in ops if o["op"] not in ("COPY_IN", "COPY_OUT")]
                span = union_length(compute)
                task_span = union_length((t["start"], t["end"]) for t in core["tasks"])
                first = min((o["start"] for o in ops), default=0)
                end = max((o["end"] for o in ops), default=0)
                cores.append({"core": core["core_id"], "first_op": first, "last_op": end,
                              "task_count": len(core["tasks"]), "task_span": task_span,
                              "before_between_task_gap": end-task_span,
                              "pipe_busy_cycles": work, "compute_union_cycles": span,
                              "mv_overlap_cycles": work["PIPE_M"]+work["PIPE_V"]-span,
                              "compute_idle_before_global_end": result["makespan"]-span,
                              "last_ops": [{k:o[k] for k in ("op_id","op","pipe","start","end")} for o in sorted(ops,key=lambda o:o["end"])[-3:]]})
            routes = result.get("cross_core_transfers", [])
            waits = sorted(r["copy_in_start"]-r["copy_in_release"] for r in routes)
            memory = result.get("step3_by_core", result.get("step3_by_task", {}))
            evidence[path] = {"result_path": path, "result_sha256": ev["result_sha256"],
                              "verified_makespan": result["makespan"], "cores": cores,
                              "task_dependency_count": len(result.get("task_dependencies", [])),
                              "cross_route_count": len(routes),
                              "route_read_release_wait_median": waits[len(waits)//2] if waits else None,
                              "route_read_release_wait_max": max(waits, default=0),
                              "late_routes": sorted(routes,key=lambda r:r["copy_in_end"], reverse=True)[:3],
                              "memory_dependency_count": sum(v.get("memory_dependency_count",0) for v in memory.values()),
                              "cache_stats": result.get("cache_stats"),
                              "cache_insertions_with_evictions": sum(e.get("event")=="insert" and bool(e.get("evicted_tensor_ids")) for e in result.get("cache_events", [])),
                              "scope": "descriptive intervals; idle/wait sums are not critical-path causal attribution"}
        reference = slot["before_trace"] if stage == "trace" else slot["before_cache"] if stage == "cache" else slot["stage_best"].get("component")
        changed = None
        if reference:
            a, _ = assignment(reference); b, _ = assignment(ev)
            assert set(a) == set(b)
            changed = sum(a[v] != b[v] for v in a)
        links.append({"case":slot["case"],"problem":slot["problem"],"num_cores":slot["num_cores"],"stage":stage,
                      "name":ev["name"],"result_path":path,"changed_core_ops_vs_prior":changed,
                      "subgraphs":assignment(ev)[1],"metrics":ev["metrics"]})
    out["timeline_evidence"] = evidence
    out["timeline_links"] = links
    fixed_assignment_checks = []
    for slot in out["rows"]:
        winner = slot["selected"]
        if (slot["complete"] and slot["problem"] == 2 and winner["stage"] == "trace"
                and winner["metadata"].get("assignment_unchanged")):
            a, _ = assignment(slot["before_trace"])
            b, _ = assignment(winner)
            fixed_assignment_checks.append({"case": slot["case"], "num_cores": slot["num_cores"],
                "name": winner["name"], "changed_core_ops": sum(a[o] != b[o] for o in a),
                "before": slot["before_trace"]["metrics"]["makespan"],
                "after": winner["metrics"]["makespan"]})
    out["p2_fixed_assignment_winner_checks"] = fixed_assignment_checks
    OUT.write_text(json.dumps(out,ensure_ascii=False,indent=2))
    print(json.dumps({"snapshot_utc":out["snapshot_utc"],"source_results_hash_verified":len(evidence),"stage_references":len(links),"priority_cases":targets},ensure_ascii=False))


if __name__ == "__main__":
    if "--timelines" in sys.argv:
        enrich_timelines()
    else:
        take_snapshot()
