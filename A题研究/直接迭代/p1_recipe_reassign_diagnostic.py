"""Two-call diagnostic: reassign after the frozen tensor recipe's final split."""
from collections import defaultdict, Counter
from pathlib import Path
import argparse
import hashlib
import time

from common_run import DATA, GraphIR, atomic_json, evaluate, score
from p1_recipe_bootstrap import candidate, generation_limit
from p1_selective import topological_order, _toposort_blocks, block_views, _assign


def run(out):
    out = Path(out); out.mkdir(parents=True, exist_ok=False)
    specs = [("tensor_merge", 8), ("tensor_work_fraction", 16)]
    atomic_json(out/"manifest.json", dict(case="case_085", problem=1, cores=5,
        specs=specs, budget=2, timeout=60, generation_timeout=30,
        source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        scope="registered developmental assignment control; original graph only; frozen recipe unchanged"))
    started = time.monotonic(); ir = GraphIR.from_path(DATA/"case_085.json")
    calls, errors = [], []
    for spec in specs:
        t = time.monotonic()
        try:
            with generation_limit(30):
                original = candidate(ir, 5, spec)
                groups = defaultdict(list)
                for op, sg in original["plan"]["node_to_subgraph"].items():
                    groups[sg].append(int(op))
                order = topological_order(ir, "stable_id")
                blocks = _toposort_blocks(ir, list(groups.values()), order)
                plan, proxy = _assign(ir, blocks, block_views(ir, blocks), 5, 5, "eft")
                original_groups = {frozenset(x) for x in groups.values()}
                new_groups = defaultdict(list)
                for op, sg in plan["node_to_subgraph"].items(): new_groups[sg].append(int(op))
                if original_groups != {frozenset(x) for x in new_groups.values()}:
                    raise AssertionError("diagnostic changed partition")
                old_owner = {sg: core for core, tasks in enumerate(original["plan"]["core_schedules"]) for sg in tasks}
                new_owner = {sg: core for core, tasks in enumerate(plan["core_schedules"]) for sg in tasks}
                moved = sum(old_owner[original["plan"]["node_to_subgraph"][op]] != new_owner[sg]
                            for op, sg in plan["node_to_subgraph"].items())
        except Exception as exc:
            errors.append(dict(spec=spec, error=repr(exc), seconds=time.monotonic()-t)); continue
        generation = time.monotonic()-t
        record = evaluate(ir.path, plan, 1, out/"evaluations", timeout=60, config_path=DATA/"config.txt")
        calls.append(dict(name=original["name"]+"_final_eft", record=record,
            metadata=dict(partition_preserved=True, moved_ops=moved, task_count=len(blocks),
                max_task_ops=max(map(len, blocks)), proxy_end=proxy,
                generation_seconds=generation, original_metadata=original["metadata"],
                scope="assignment plus topological Task-ID/order encoding control; no partition change")))
        atomic_json(out/"summary.json", dict(case="case_085", problem=1, cores=5,
            budget=2, logical_calls=len(calls), calls=calls, errors=errors,
            elapsed_seconds=time.monotonic()-started, complete=False))
    s = dict(case="case_085", problem=1, cores=5, budget=2, logical_calls=len(calls),
             calls=calls, errors=errors, elapsed_seconds=time.monotonic()-started, complete=True)
    atomic_json(out/"summary.json", s)
    for call in calls:
        print(dict(name=call["name"], status=call["record"]["status"],
            score=score(call["record"]) if call["record"]["status"] == "success" else None,
            moved_ops=call["metadata"]["moved_ops"],
            seconds=call["record"]["elapsed_seconds"]), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--out", type=Path, required=True)
    run(parser.parse_args().out)
