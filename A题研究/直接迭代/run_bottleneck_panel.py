"""Frozen warm development panel: eight charged calls/cell including replay.

Fresh official trace is used only after charging its evaluation. All raw attempts
and failed candidates are retained. This is not a from-scratch algorithm score.
"""
import argparse
import csv
import gzip
import hashlib
import json
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from common_run import GraphIR, DATA, atomic_json, read_json, run_candidate, score
from p1_task_refine import split_large, view
from p1_selective import topological_order
from p1_bottleneck_diagnose import diagnose, regions, region_trace
from p1_bottleneck_repartition import generate, prepare, schedule, merge_region

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CELLS = [('043', 4), ('051', 5), ('047', 5), ('075', 5), ('064', 5)]


def plan_key(plan):
    return hashlib.sha256(json.dumps(plan, separators=(',', ':')).encode()).hexdigest()


def run_cell(spec):
    cell, out = spec
    out = Path(out) / cell['case']; out.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter(); deadline = started + 180
    ir = GraphIR.from_path(DATA / (cell['case'] + '.json')); incumbent = read_json(ROOT / cell['plan'])
    attempts, seen, skipped = [], set(), []
    def evaluate(name, plan, metadata=None):
        signature = plan_key(plan)
        if signature in seen:
            skipped.append({'name': name, 'reason': 'duplicate', 'plan_key': signature}); return None
        if len(attempts) >= 8 or time.perf_counter() >= deadline:
            skipped.append({'name': name, 'reason': 'budget_or_deadline'}); return None
        seen.add(signature)
        atomic_json(out / 'plans' / (name + '.json'), plan)
        rec = run_candidate(cell['case'], 1, cell['cores'], plan, out / 'evaluations',
                            min(60, max(.01, deadline - time.perf_counter())))
        entry = {'name': name, 'plan_path': str(out / 'plans' / (name + '.json')),
                 'metadata': metadata or {}, 'plan_key': signature, 'record': rec}
        attempts.append(entry); atomic_json(out / 'attempts.json', attempts)
        print(json.dumps({'case': cell['case'], 'name': name, 'status': rec['status'],
                          'time': rec.get('metrics', {}).get('makespan')}, ensure_ascii=False), flush=True)
        return entry
    baseline = evaluate('baseline', incumbent)
    if baseline['record']['status'] != 'success':
        raise RuntimeError('baseline replay failed')
    if baseline['record']['metrics']['makespan'] != cell['makespan']:
        raise ValueError('baseline differs from frozen cumulative result')
    raw = json.load(gzip.open(baseline['record']['result_path'], 'rt'))
    diag = diagnose(ir, incumbent, raw); selected_regions = regions(ir, incumbent, diag)
    atomic_json(out / 'diagnosis.json', diag)
    construction_start = time.perf_counter(); construction_deadline = min(deadline, construction_start + 30)
    mp, _, old_nodes, _, _ = view(ir, incumbent)
    # Control: split old Tasks by operation count, then analytical EFT. Regional
    # ownership constraints match the new methods; no uncharged trace is needed.
    if selected_regions:
        region = selected_regions[0]; split = split_large(ir, incumbent, 1024)
        _, _, split_nodes, _, _ = view(ir, split)
        members = {o for t in region for o in old_nodes[t]}
        replacement = [v for v in split_nodes.values() if v[0] in members]
        try:
            blocks, bv, fixed, preds = prepare(ir, incumbent, region, replacement, topological_order(ir, 'stable_id'))
            control, proxy = schedule(ir, incumbent, blocks, bv, fixed, preds, 1, construction_deadline)
        except (ValueError, TimeoutError) as error:
            control = None; skipped.append({'name': 'count1024_eft', 'reason': str(error)})
    else:
        control = None
    pool, generation = generate(ir, incumbent, selected_regions,
                               max(0, construction_deadline - time.perf_counter()))
    generation['regions'] = selected_regions
    generation['construction_seconds_including_control'] = time.perf_counter() - construction_start
    atomic_json(out / 'generation.json', {'summary': generation,
                'candidates': [{k: v for k, v in c.items() if k != 'plan'} for c in pool]})
    if control is not None: evaluate('count1024_eft', control, {'proxy': proxy})
    selected = []
    for family in ('branch', 'affinity'):
        choices = [c for c in pool if c['metadata']['family'] == family and c['metadata']['width'] == 1]
        if choices: selected.append(min(choices, key=lambda c: (c['metadata']['proxy'], c['name'])))
    for c in selected: evaluate(c['name'], c['plan'], c['metadata'])
    joint = []
    for c in selected:
        name = c['name'].rsplit('_w', 1)[0] + '_w4'
        counterpart = next((x for x in pool if x['name'] == name), None)
        if counterpart:
            entry = evaluate(name, counterpart['plan'], counterpart['metadata'])
            # A duplicate is already evaluated; keep its measured result for
            # choosing a composed merge even when the standalone split loses.
            if entry is None:
                entry = next((x for x in attempts if x['plan_key'] == plan_key(counterpart['plan'])), None)
            if entry and entry['record']['status'] == 'success': joint.append((entry, counterpart))
    if joint:
        entry, chosen = min(joint, key=lambda x: score(x[0]['record']))
        region = chosen['metadata']['old_tasks']
        protected = {o for o, t in mp.items() if t not in region}
        merged = merge_region(ir, chosen['plan'], protected, chosen['metadata']['target_work'] * 2,
                              min(deadline, time.perf_counter() + max(0, 30 - generation['construction_seconds_including_control'])))
        evaluate(chosen['name'] + '_merge', merged, {'parent': chosen['name'], 'parent_makespan': entry['record']['metrics']['makespan']})
    diversity = [c for c in pool if c['metadata']['width'] == 4 and plan_key(c['plan']) not in seen]
    if diversity:
        c = min(diversity, key=lambda c: (c['metadata']['proxy'], c['name']))
        evaluate(c['name'] + '_diversity', c['plan'], c['metadata'])
    best = min((x for x in attempts if x['record']['status'] == 'success'), key=lambda x: score(x['record']))
    best_raw = json.load(gzip.open(best['record']['result_path'], 'rt'))
    critical_ops = set(old_nodes[diag['heavy_tasks'][0]['task']])
    chain_ops = {o for t in diag['final_blocker_chain'] for o in old_nodes[t]}
    evidence = {label: {'baseline': region_trace(raw, ops), 'selected': region_trace(best_raw, ops)}
                for label, ops in [('old_heaviest_task', critical_ops), ('old_final_blocker_chain', chain_ops)]}
    summary = {**cell, 'selected_makespan': best['record']['metrics']['makespan'], 'selected': best['name'],
               'selected_plan': best['plan_path'], 'selected_record': best['record']['record_path'],
               'calls': len(attempts), 'fresh_calls': sum(not x['record']['cache_hit'] for x in attempts),
               'elapsed_seconds': time.perf_counter() - started, 'skipped': skipped,
               'generation': generation, 'evidence': evidence,
               'status_counts': {s: sum(x['record']['status'] == s for x in attempts)
                                 for s in {x['record']['status'] for x in attempts}}}
    atomic_json(out / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(); out = args.out.resolve(); out.mkdir(parents=True, exist_ok=False)
    rows = list(csv.DictReader((HERE / '闭环验证_20260925/累计1500配置成绩.csv').open(encoding='utf-8-sig')))
    cells = []
    for case, cores in CELLS:
        row = next(x for x in rows if x['case'] == 'case_' + case and x['problem'] == '1' and int(x['cores']) == cores)
        cells.append({'case': row['case'], 'cores': cores, 'plan': row['plan'], 'makespan': int(row['makespan'])})
    sources = list(HERE.glob('*bottleneck*.py')) + [HERE / 'p1_task_refine.py', HERE / 'common_run.py',
                HERE.parent / '精修求解器/p1_selective.py', HERE.parent / '探索/partition_candidates.py']
    protocol = {'kind': 'warm development, not from scratch', 'cells': cells, 'call_cap': 8,
                'workers': 2, 'cell_seconds': 180, 'evaluation_seconds': 60, 'construction_seconds': 30,
                'seed': 17, 'git': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
                'selection': 'min greedy proxy per family; evaluate same partition with width4; '
                             'merge better measured joint candidate, regardless of intermediate loss; '
                             'one cheapest distinct width4 diversity; duplicates do not consume calls',
                'control_adjustment': 'Analytical EFT with outside constraints replaces observed-duration reschedule: '
                                      'a changed partition has no matching trace without another charged evaluation.'}
    atomic_json(out / 'manifest.json', protocol)
    with ProcessPoolExecutor(max_workers=2) as executor:
        summaries = list(executor.map(run_cell, [(c, str(out)) for c in cells]))
    gate = sum(s['selected_makespan'] <= .95 * s['makespan'] for s in summaries) >= 2
    atomic_json(out / 'summary.json', {'cells': summaries, 'numerical_development_gate': gate,
                'note': 'Trace evidence must also support bottleneck improvement before promotion.',
                'total_calls': sum(s['calls'] for s in summaries)})


if __name__ == '__main__':
    main()
