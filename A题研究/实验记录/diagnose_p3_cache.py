#!/usr/bin/env python3
"""Read existing pilot artifacts once. No solver/official/runs files are changed."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path


def load(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def compressed(record):
    with gzip.open(record["result_path"], "rt", encoding="utf-8") as stream:
        return json.load(stream)


def duration_union(intervals):
    total, left, right = 0, None, None
    for start, end in sorted(intervals):
        if left is None:
            left, right = start, end
        elif start > right:
            total += right - left
            left, right = start, end
        else:
            right = max(right, end)
    return total + (right - left if left is not None else 0)


def analyze(p2, p3, p3_record):
    events = p3["cache_events"]
    accesses, inserts = defaultdict(list), defaultdict(list)
    counts, counted_bytes = Counter(), Counter()
    seen, ever_inserted, resident = set(), set(), set()
    evictions = []
    all_ops = {(core["core_id"], op["op_id"]): op
               for core in p3["per_core_timeline"] for op in core["ops"]}
    for sequence, event in enumerate(events):
        kind, tid = event["event"], event["tensor_id"]
        if kind == "insert":
            ever_inserted.add(tid)
            resident.add(tid)
            inserts[tid].append(event)
            for old in event["evicted_tensor_ids"]:
                resident.discard(old)
                evictions.append({"time": event["time"], "tensor_id": old, "inserting_tensor": tid})
            continue
        event = dict(event)
        op = all_ops[(event["core_id"], event["op_id"])]
        event["end"] = op["end"]
        event["duration"] = op["duration"]
        event["subgraph_id"] = op["subgraph_id"]
        event["event_index"] = sequence
        if kind == "hit":
            category = "hit"
            assert tid in resident
        elif event["size_bytes"] > p3["cache_capacity_bytes"]:
            category = "oversize_miss"
        elif tid not in seen:
            category = "first_access_miss"
        elif tid not in ever_inserted:
            category = "repeat_before_first_insert_miss"
        else:
            assert tid not in resident
            category = "post_eviction_miss"
        event["category"] = category
        counts[category] += 1
        counted_bytes[category] += event["size_bytes"]
        accesses[tid].append(event)
        seen.add(tid)
    repeated = []
    for tid, group in accesses.items():
        if len(group) > 1:
            repeated.append({"tensor_id": tid, "size_bytes": group[0]["size_bytes"],
                             "access_count": len(group), "cores": sorted({e["core_id"] for e in group}),
                             "potential_repeat_bytes": sum(e["size_bytes"] for e in group[1:]),
                             "first_insert": inserts[tid][0]["time"] if inserts[tid] else None,
                             "accesses": group, "insert_count": len(inserts[tid])})
    repeated.sort(key=lambda x: (-x["potential_repeat_bytes"], x["tensor_id"]))
    pipe_work = defaultdict(lambda: defaultdict(int))
    ddr_intervals, compute_entries = [], []
    per_core_changes = []
    original_ops = {(core["core_id"], op["op_id"]): op
                    for core in p2["per_core_timeline"] for op in core["ops"]}
    for core in p3["per_core_timeline"]:
        for op in core["ops"]:
            if op["op"] not in ("COPY_IN", "COPY_OUT"):
                pipe_work[core["core_id"]][op["pipe"]] += op["duration"]
                compute_entries.append((op["end"], core["core_id"], op["op_id"], op["start"], op["pipe"]))
            if op.get("memory_path") == "DDR":
                ddr_intervals.append((op["start"], op["end"]))
        p2core = next(c for c in p2["per_core_timeline"] if c["core_id"] == core["core_id"])
        per_core_changes.append({"core_id": core["core_id"],
                                 "p2_end": max((op["end"] for op in p2core["ops"]), default=0),
                                 "p3_end": max((op["end"] for op in core["ops"]), default=0),
                                 "compute_pipe_work": dict(pipe_work[core["core_id"]]),
                                 "changed_op_times": sum((op["start"], op["end"]) !=
                                     (original_ops[(core["core_id"], op["op_id"])]["start"],
                                      original_ops[(core["core_id"], op["op_id"])]["end"])
                                     for op in core["ops"])})
    max_pipe_work = max((work for pipes in pipe_work.values() for work in pipes.values()), default=0)
    total_read = p3["cache_stats"]["hit_bytes"] + p3["cache_stats"]["miss_bytes"]
    unique_bytes = sum(group[0]["size_bytes"] for group in accesses.values())
    plan = load(p3_record["plan_path"])
    graph = load(p3_record["graph_path"])
    eligible = set(map(int, plan["node_to_subgraph"]))
    producer, consumer = defaultdict(set), defaultdict(set)
    for edge in graph["edges"]:
        if edge["source"] in eligible:
            producer[edge["target"]].add(edge["source"])
        if edge["target"] in eligible:
            consumer[edge["source"]].add(edge["target"])
    input_tids = [tid for tid in consumer if not producer[tid]]
    shared_input_tids = [tid for tid in input_tids if len(consumer[tid]) > 1]
    shared_cross_core_tids = [tid for tid in input_tids
                             if len({e["core_id"] for e in accesses.get(tid, [])}) > 1]
    return {
        "makespan_p2": p2["makespan"], "makespan_p3": p3["makespan"],
        "hardware_ratio": p2["makespan"] / p3["makespan"],
        "active_cores": sum(bool(order) for order in plan["core_schedules"]),
        "cache_stats": p3["cache_stats"], "access_categories": dict(counts),
        "access_category_bytes": dict(counted_bytes),
        "distinct_read_tensors": len(accesses), "repeated_read_tensors": len(repeated),
        "cross_core_repeated_tensors": sum(len(x["cores"]) > 1 for x in repeated),
        "all_read_bytes": total_read, "unique_read_bytes": unique_bytes,
        "potential_repeat_bytes": total_read - unique_bytes,
        "potential_repeat_fraction": (total_read - unique_bytes) / total_read if total_read else 0,
        "original_input_tensors": len(input_tids),
        "original_inputs_with_multiple_compute_consumers": len(shared_input_tids),
        "original_inputs_actually_read_by_multiple_cores": len(shared_cross_core_tids),
        "fifo_eviction_count": len(evictions), "fifo_evictions_first10": evictions[:10],
        "max_cache_used_bytes": max((e["used_bytes"] for e in events if e["event"] == "insert"), default=0),
        "max_compute_pipe_work": max_pipe_work,
        "max_compute_pipe_busy_fraction": max_pipe_work / p3["makespan"],
        "ddr_busy_cycles_union": duration_union(ddr_intervals),
        "ddr_busy_fraction": duration_union(ddr_intervals) / p3["makespan"],
        "per_core": per_core_changes, "last_compute_ops": sorted(compute_entries, reverse=True)[:8],
        "largest_repeated_tensors": repeated[:6],
        "data_movement_bytes": p3["data_movement_bytes"],
        "p2_result_path": None, "p3_result_path": p3_record["result_path"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", nargs="+", default=["pilot_v1", "pilot_fullpool_v2"])
    args = parser.parse_args()
    out = Path(__file__).resolve().parent
    root = out.parent / "solver/runs"
    snapshot_time = datetime.now(timezone.utc).isoformat()
    rows, inputs, skipped, progress = [], [], [], {}
    # Snapshot file inventory once; do not wait for or poll the pipeline.
    inventory = [(run, sorted((root / run / "results").glob("case_*/*_cache_pair_n*_seed0.json")))
                 for run in args.runs]
    for run, files in inventory:
        progress[run] = load(root / run / "progress.json") if (root / run / "progress.json").is_file() else None
        for path in files:
            payload = path.read_bytes()
            pair = json.loads(payload)
            inputs.append({"path": str(path), "sha256": hashlib.sha256(payload).hexdigest()})
            cells = pair["cells"]
            if any(cells.get(k, {}).get("status") != "success" for k in ("t2_pi2", "t3_pi2")):
                skipped.append(str(path)); continue
            p2, p3 = compressed(cells["t2_pi2"]), compressed(cells["t3_pi2"])
            row = analyze(p2, p3, cells["t3_pi2"])
            row.update(run=run, case=pair["case"], method=pair["method"], num_cores=pair["num_cores"],
                       pair_path=str(path), p2_result_path=cells["t2_pi2"]["result_path"],
                       plan_path=cells["t3_pi2"]["plan_path"], ratios=pair.get("ratios"))
            if cells.get("t3_pi3", {}).get("status") == "success":
                row["p3_selected_metrics"] = cells["t3_pi3"]["metrics"]
            rows.append(row)
    metadata = {"snapshot_utc": snapshot_time, "progress_at_snapshot": progress,
                "scope": "read-only diagnostics of saved pilot results; no new evaluation",
                "pair_records_read": len(inputs), "analyzed_rows": len(rows), "skipped": skipped,
                "inputs": inputs, "rows": rows}
    (out / "P3缓存诊断.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    table = []
    for row in rows:
        table.append({k: row[k] for k in ("run", "case", "method", "num_cores", "active_cores",
                      "makespan_p2", "makespan_p3", "hardware_ratio", "original_input_tensors",
                      "original_inputs_with_multiple_compute_consumers", "original_inputs_actually_read_by_multiple_cores",
                      "repeated_read_tensors", "all_read_bytes", "potential_repeat_bytes", "potential_repeat_fraction",
                      "fifo_eviction_count", "max_cache_used_bytes", "max_compute_pipe_busy_fraction", "ddr_busy_fraction")}
                     | {"hit_bytes": row["cache_stats"]["hit_bytes"],
                        "hit_rate": row["cache_stats"]["hit_rate"],
                        "miss_first": row["access_categories"].get("first_access_miss", 0),
                        "miss_inflight_repeat": row["access_categories"].get("repeat_before_first_insert_miss", 0),
                        "miss_after_eviction": row["access_categories"].get("post_eviction_miss", 0)})
    if table:
        with (out / "P3缓存诊断汇总.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(table[0])); writer.writeheader(); writer.writerows(table)
    print(json.dumps({"snapshot_utc": snapshot_time, "rows": len(rows), "skipped": len(skipped)}, ensure_ascii=False))
    for run in args.runs:
        selection = {}
        for row in rows:
            if row["run"] != run or row["num_cores"] != 5: continue
            key = row["case"]
            if key not in selection or row["makespan_p2"] < selection[key]["makespan_p2"]:
                selection[key] = row
        for key, row in sorted(selection.items()):
            print(json.dumps({"run": run, "case": key, "method": row["method"], "T2": row["makespan_p2"],
                              "T3": row["makespan_p3"], "active": row["active_cores"],
                              "miss": row["access_categories"], "hit_rate": row["cache_stats"]["hit_rate"],
                              "repeat_bytes": row["potential_repeat_bytes"], "read_bytes": row["all_read_bytes"],
                              "fifo_evictions": row["fifo_eviction_count"], "max_pipe_busy": row["max_compute_pipe_busy_fraction"],
                              "ddr_busy": row["ddr_busy_fraction"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
