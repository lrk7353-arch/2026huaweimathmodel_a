"""Pure necessary P1 DDR work; independent research, not in frozen v3.

Counts Task boundary COPY exactly as _build_scene_a_tasks before spill insertion.
All these ops survive Step2. Each has a DDR endpoint and consumes nominal work
max(1,ceil(size/60)) from the single pool shared by all cores and both directions.
This is a lower bound on successful execution, not an achievable-time estimate.
"""
from collections import defaultdict
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from solver.plan import validate_plan


def boundary_ddr_lower_bound(ir, plan, *, bandwidth=60):
    if type(bandwidth) is not int or bandwidth != 60:
        raise ValueError("this audited P1 bound uses official bandwidth 60")
    validate_plan(ir, plan)
    mapping = {int(op): task for op, task in plan["node_to_subgraph"].items()}
    producers, consumers = defaultdict(set), defaultdict(set)
    for edge in ir.graph["edges"]:
        src, dst = edge["source"], edge["target"]
        if src in ir.ops and dst in ir.tensors:
            producers[dst].add(src)
        elif src in ir.tensors and dst in ir.ops:
            consumers[src].add(dst)
    full = dict(copy_in_count=0, copy_out_count=0, copy_in_bytes=0,
                copy_out_bytes=0, nominal_ddr_cycles=0)
    cross = dict(full)
    tensor_rows = []
    for tid in sorted(ir.tensors):
        src_tasks = {mapping[o] for o in producers[tid] if o in mapping}
        dst_tasks = {mapping[o] for o in consumers[tid] if o in mapping}
        has_copy_out = any(ir.ops[o]["op"] == "COPY_OUT" for o in consumers[tid])
        reads = dst_tasks - src_tasks
        writes = {s for s in src_tasks if has_copy_out or not dst_tasks or dst_tasks - {s}}
        cross_reads = reads if src_tasks else set()
        cross_writes = {s for s in src_tasks if dst_tasks - {s}}
        size = ir.tensors[tid]["size"]
        duration = max(1, (size + bandwidth - 1) // bandwidth)
        for result, ins, outs in ((full, reads, writes), (cross, cross_reads, cross_writes)):
            result["copy_in_count"] += len(ins)
            result["copy_out_count"] += len(outs)
            result["copy_in_bytes"] += len(ins) * size
            result["copy_out_bytes"] += len(outs) * size
            result["nominal_ddr_cycles"] += (len(ins) + len(outs)) * duration
        if reads or writes:
            tensor_rows.append(dict(tensor_id=tid, size=size, nominal_copy_cycles=duration,
                copy_in_tasks=sorted(reads), copy_out_tasks=sorted(writes),
                conservative_cross_in_tasks=sorted(cross_reads),
                conservative_cross_out_tasks=sorted(cross_writes)))
    for result in (full, cross):
        result["total_bytes"] = result["copy_in_bytes"] + result["copy_out_bytes"]
    return {"schema_version": 1, "problem": 1, "bandwidth": bandwidth,
        "lower_bound": full["nominal_ddr_cycles"], "mandatory_boundary": full,
        "conservative_compute_cross_task_subset": cross, "tensors": tensor_rows,
        "scope": "mandatory Task boundary COPY before spill; includes root inputs and final outputs; excludes spill and overlap-independent compute/wait; all directions/cores share one nominal DDR work pool",
        "safe_pruning_rule": "max(this bound, other independently necessary bound) strictly greater than verified incumbent makespan; never sum overlapping bounds"}
