#!/usr/bin/env python3
"""Portable launch helper; installed as run_project.py at handoff root."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=('quick', 'solve'), default='quick')
    p.add_argument('--case', type=int, choices=range(1, 101), default=71)
    p.add_argument('--problem', type=int, choices=(1, 2, 3), default=2)
    p.add_argument('--cores', type=int, choices=range(1, 6), default=5)
    p.add_argument('--run-dir', type=Path)
    p.add_argument('--budget', type=int)
    p.add_argument('--time-budget', type=float, default=120)
    p.add_argument('--timeout', type=float)
    p.add_argument('--seed', type=int, default=17)
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    if sys.version_info < (3, 12): p.error('Please use Python 3.12 or newer.')
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / 'delivery_manifest.json').read_text(encoding='utf-8'))
    index = json.loads((root / 'delivery_index.json').read_text(encoding='utf-8'))
    case = f'case_{a.case:03d}'
    graph = root / '选题分析/A题附件/data' / (case + '.json')
    config = graph.parent / 'config.txt'
    if sha(graph) != manifest['graphs_sha256'][case] or sha(config) != manifest['config_sha256']:
        p.error('Official graph/config differs from the handoff snapshot.')
    official = root / '选题分析/A题附件/code'
    if {f.name:sha(f) for f in official.glob('*.py')} != manifest['official_py_sha256']:
        p.error('Official evaluator has been modified.')
    out = (a.run_dir or root.parent / 'A题队友实验' / (case + f'_p{a.problem}_n{a.cores}_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))).expanduser().resolve()
    if out == root or root in out.parents or out.exists():
        p.error('Use a new run directory outside the unpacked package.')
    budget = a.budget if a.budget is not None else (9 if a.mode == 'quick' else 90)
    if budget < 1: p.error('budget must be positive')
    script = root / 'A题研究/精修求解器' / ('quick_refine.py' if a.mode == 'quick' else 'solve.py')
    command = [sys.executable, '-B', str(script), str(graph), '-p', str(a.problem), '-n', str(a.cores),
               '--config', str(config), '--run-dir', str(out), '--max-evaluations', str(budget), '--seed', str(a.seed)]
    if a.mode == 'quick':
        if a.problem == 1: p.error('Quick local refinement supports P2/P3; use --mode solve for P1.')
        rows = [r for r in index if (r['case'], r['problem'], r['num_cores']) == (case, a.problem, a.cores)]
        if not rows: p.error('This snapshot lacks that starting plan; use --mode solve to build a fresh one.')
        row = rows[0]
        plan = (root / row['plan_path']).resolve()
        if root not in plan.parents or sha(plan) != row['plan_file_sha256']:
            p.error('Starting plan missing or changed; preserve original plans and write new results separately.')
        command += ['--incumbent-plan', str(plan), '--time-budget', str(a.time_budget),
                    '--timeout', str(a.timeout if a.timeout is not None else 30)]
    elif a.timeout is not None:
        command += ['--timeout', str(a.timeout)]
    print(json.dumps({'mode':a.mode, 'output':str(out), 'command':command}, ensure_ascii=False), flush=True)
    if a.dry_run: return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.call(command, cwd=str(root))


if __name__ == '__main__':
    raise SystemExit(main())
