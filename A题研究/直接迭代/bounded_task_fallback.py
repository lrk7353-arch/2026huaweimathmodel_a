"""Bound Task compilation size with a legal topological partition (P1 only)."""
import argparse
from pathlib import Path

from common_run import DATA, GraphIR, atomic_json, evaluate, score, validate_plan
from unified_structure import Structure


def plan(ir, cores, cap=512):
    order = Structure(ir).topo
    blocks = [order[i:i+cap] for i in range(0, len(order), cap)]
    result = dict(node_to_subgraph={str(o): i for i, b in enumerate(blocks) for o in b},
                  core_schedules=[[i for i in range(len(blocks)) if i % cores == c] for c in range(cores)])
    validate_plan(ir, result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--case', required=True)
    p.add_argument('--cores', type=int, default=5); p.add_argument('--cap', type=int, default=512)
    p.add_argument('--out', type=Path, required=True); a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    ir = GraphIR.from_path(DATA/(a.case+'.json')); candidate = plan(ir, a.cores, a.cap)
    r = evaluate(DATA/(a.case+'.json'), candidate, 1, a.out/'evaluations', timeout=60, config_path=DATA/'config.txt')
    atomic_json(a.out/'probe.json', dict(case=a.case, cores=a.cores, cap=a.cap, record=r))
    print(dict(status=r['status'], score=score(r) if r['status']=='success' else None,
               seconds=r['elapsed_seconds']), flush=True)
