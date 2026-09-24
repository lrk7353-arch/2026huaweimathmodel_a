"""Independent fine-depth-band P1 probe; pure generation, outside frozen v3.

Depth width 1 is an actual compute antichain. Wider bands still require SCC
coarsening after M/V bin packing. Group labels never fix final Task core IDs.
"""
from collections import defaultdict
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import p1_convex_regions as regions


def depth_packed_groups(ir, num_cores, width):
    if type(width) is not int or width not in (1, 2, 4, 8):
        raise ValueError("only preregistered depth widths 1,2,4,8 are supported")
    order = regions.topological_order(ir, "stable_id")
    depth, layers, bands = {}, defaultdict(list), defaultdict(list)
    for op in order:
        depth[op] = 1 + max((depth[p] for p in ir.predecessors[op]), default=-1)
        layers[depth[op]].append(op)
        bands[depth[op] // width].append(op)
    groups, group_labels = [], []
    for band, members in sorted(bands.items()):
        count = min(num_cores, len(members))
        loads = [{p: 0 for p in ("PIPE_M", "PIPE_V", "PIPE_MTE2", "PIPE_MTE3")} for _ in range(count)]
        bins = [[] for _ in range(count)]
        for op in sorted(members, key=lambda o: (-max(1, ir.ops[o]["cycles"]), o)):
            pipe, cost = ir.ops[op]["pipe"], max(1, ir.ops[op]["cycles"])
            def score(bin_id):
                updated = dict(loads[bin_id]); updated[pipe] += cost
                return max(updated.values()), sum(updated.values()), len(bins[bin_id]), bin_id
            bin_id = min(range(count), key=score)
            loads[bin_id][pipe] += cost
            bins[bin_id].append(op)
        for label, bucket in enumerate(bins):
            if bucket:
                groups.append(bucket)
                group_labels.append((band, label))
    if width == 1:
        if any(depth[o] == depth[n] for o in order for n in ir.successors[o]):
            raise AssertionError("integer dependency depth is not an antichain layering")
    return order, groups, {"width": width, "dependency_levels": len(layers),
        "max_original_layer_width": max(map(len, layers.values()), default=0),
        "nonempty_bands": len(bands), "group_labels": group_labels,
        "bin_packing": "descending compute cycles, choose minimum max per-pipe load then total work/bin count/id; at most N bins per band",
        "width_one_has_no_intraband_compute_dependencies": width == 1}


def generate_depth_band_candidates(ir, num_cores, seed=17):
    if type(num_cores) is not int or num_cores not in range(1, 6) or type(seed) is not int:
        raise ValueError("1..5 integer cores and integer seed required")
    candidates, seen = [], {}
    diagnostics = {"family": "fine_integer_depth_bands", "official_calls": 0,
                   "widths": [1, 2, 4, 8], "num_cores": num_cores, "seed": seed,
                   "seed_scope": "recorded only; packing uses deterministic cost and original-ID ties",
                   "pruning_performed": False, "aliases": []}
    if not ir.compute_ids:
        return candidates, diagnostics
    for width in diagnostics["widths"]:
        order, groups, packing = depth_packed_groups(ir, num_cores, width)
        blocks, scc = regions._scc_coarsen(ir, groups, order)
        if width == 1 and scc["groups_eliminated"] != 0:
            raise AssertionError("true antichain bands unexpectedly needed SCC merging")
        for component in scc["merged_group_members"]:
            if len({packing["group_labels"][index][0] for index in component}) != 1:
                raise AssertionError("SCC crossed a monotone depth band")
        view = regions.block_views(ir, blocks)
        plan, proxy = regions._assign(ir, blocks, view, num_cores, num_cores, "eft")
        regions.validate_plan(ir, plan)
        lb = regions.task_lower_bound(ir, plan)
        # This supplement may group independent WCCs in a shared depth band.
        # The generic diagnostics helper intentionally prohibits that; compute
        # a smaller transparent DAG view here rather than suppress its check.
        degree, path, depth_tasks = [], [], defaultdict(list)
        for bid in range(len(blocks)):
            degree.append(1 + max((degree[p] for p in view["preds"][bid]), default=-1))
            path.append(lb["task_duration_bounds"][bid] + max((path[p] for p in view["preds"][bid]), default=0))
            depth_tasks[degree[-1]].append(bid)
        signature = regions.object_digest(plan)
        name = "depth_width{}_mvpack_p1eft_n{}".format(width, num_cores)
        metadata = {"family": "p1_fine_depth_band", "width": width, "packing": packing, "scc": scc,
            "task_count": len(blocks), "actual_active_cores": sum(bool(v) for v in plan["core_schedules"]),
            "max_equal_task_depth_antichain": max(map(len, depth_tasks.values()), default=0),
            "data_dag_compute_bound_path": max(path, default=0),
            "task_proxy_end": proxy, "plan_lower_bound": lb,
            "direct_incoming_boundary_bytes": sum(view["boundary_bytes"]),
            "boundary_read_plus_write_proxy_bytes": 2 * sum(view["boundary_bytes"]),
            "proxy_scope": "same frozen P1 EFT duration proxy; 2*incoming bytes approximates boundary read/write and excludes spill, final outputs, dynamic contention; low LB not achievable-time claim",
            "also_generated_as": []}
        if signature in seen:
            seen[signature]["metadata"]["also_generated_as"].append(name)
            diagnostics["aliases"].append({"name": name, "same_as": seen[signature]["name"]})
        else:
            row = {"name": name, "plan": plan, "metadata": metadata}
            candidates.append(row); seen[signature] = row
    diagnostics["generated_unique"] = len(candidates)
    return candidates, diagnostics
