"""Cold P1 candidates with bounded Tasks and component-aware scheduling.

Independent components are never joined. Earliest-start bands preserve DAG
order; every band component is capped in a topological order. No case identity,
historical plan, score, or hardware measurements enter candidate construction.
"""
from collections import defaultdict
import argparse
from pathlib import Path
import time

from common_run import DATA, GraphIR, atomic_json, evaluate, score
from p1_selective import _toposort_blocks, _phase_blocks, _assign, block_views
from partition_candidates import topological_order


def candidate(ir, cores, cap=512, bands=0, ordering="critical_path"):
    if cores not in range(1, 6) or cap < 1 or bands < 0:
        raise ValueError("invalid core count, cap, or bands")
    order = topological_order(ir, ordering)
    by_component = defaultdict(list)
    for op in order:
        by_component[ir.component_by_op[op]].append(op)
    blocks = []
    for nodes in by_component.values():
        pieces = _phase_blocks(ir, nodes, nodes, bands) if bands else [nodes]
        rank = {o: i for i, o in enumerate(nodes)}
        for piece in pieces:
            piece = sorted(piece, key=rank.__getitem__)
            blocks.extend(piece[i:i+cap] for i in range(0, len(piece), cap))
    blocks = _toposort_blocks(ir, blocks, order)
    plan, proxy = _assign(ir, blocks, block_views(ir, blocks), cores, cores, "eft")
    return dict(name=f"bounded_component_cap{cap}_bands{bands}_{ordering}", plan=plan,
                metadata=dict(cap=cap, bands=bands, ordering=ordering,
                              tasks=len(blocks), max_task_ops=max(map(len, blocks), default=0),
                              proxy_end=proxy, scope="approximate task costs; ranking only"))


def candidates(ir, cores):
    for cap, bands in [(512, 0), (512, max(2, 2*cores)),
                       (1024, max(2, 2*cores)), (256, max(2, 2*cores))]:
        yield candidate(ir, cores, cap, bands)


def run(case, cores, out, budget=4, seconds=240, timeout=60):
    if budget < 1 or seconds <= 0 or timeout <= 0:
        raise ValueError('positive budgets required')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    ir = GraphIR.from_path(DATA/(case+".json"))
    calls, errors, seen, best = [], [], set(), None
    generation = 0.
    stream = candidates(ir, cores)
    while len(calls) < budget and time.monotonic()-start < seconds:
        t = time.monotonic()
        try:
            c = next(stream)
        except StopIteration:
            break
        except Exception as exc:
            errors.append(repr(exc))
            break
        generation += time.monotonic()-t
        # Preserve insertion order, since official scheduling may use it.
        signature = repr(c["plan"])
        if signature in seen:
            continue
        seen.add(signature)
        remaining = seconds-(time.monotonic()-start)
        if remaining <= 0:
            break
        r = evaluate(ir.path, c["plan"], 1, out/"evaluations",
                     timeout=min(timeout, remaining), config_path=DATA/"config.txt")
        calls.append(dict(name=c["name"], metadata=c["metadata"], record=r))
        if r["status"] == "success" and (best is None or score(r) < score(best)):
            best = r
            atomic_json(out/"best.plan.json", c["plan"])
        result = dict(case=case, problem=1, num_cores=cores, budget=budget,
                      logical_calls=len(calls), calls=calls, best_record=best,
                      generation_seconds=generation, errors=errors,
                      elapsed_seconds=time.monotonic()-start,
                      scope="cold construction probe; no historical incumbent; not full portfolio")
        atomic_json(out/"summary.json", result)
    # Include generation of candidates that were duplicate or never evaluated.
    result = dict(case=case, problem=1, num_cores=cores, budget=budget,
                  logical_calls=len(calls), calls=calls, best_record=best,
                  generation_seconds=generation, errors=errors,
                  elapsed_seconds=time.monotonic()-start, complete=True,
                  scope='cold construction probe; no historical incumbent; not full portfolio')
    atomic_json(out/'summary.json', result)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--case", type=int, required=True)
    p.add_argument("--cores", type=int, default=5)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    s = run(f"case_{a.case:03d}", a.cores, a.out)
    print(dict(case=a.case, calls=s["logical_calls"],
               best=score(s["best_record"]) if s["best_record"] else None,
               seconds=s["elapsed_seconds"]), flush=True)
