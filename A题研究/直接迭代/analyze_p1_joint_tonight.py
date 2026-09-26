"""Read existing official outputs; no evaluation, compilation or plan mutation."""
import argparse, gzip, itertools, json
from collections import defaultdict
from pathlib import Path
from common_run import DATA, GraphIR, atomic_json, read_json, validate_plan, write_csv
from p1_joint_frontier import score_plan
from run_p1_relay_probe import assert_trace


def local_duration_replay(plan, raw):
    """Max-plus schedule using paid isolated Task durations; diagnostic only."""
    owner = {t: c for c, seq in enumerate(plan['core_schedules']) for t in seq}
    preds = {t: set() for t in owner}
    for edge in raw['task_dependencies']: preds[edge['target']].add(edge['source'])
    previous = {b: a for seq in plan['core_schedules'] for a, b in zip(seq, seq[1:])}
    deps = {t: ps | ({previous[t]} if t in previous else set()) for t, ps in preds.items()}
    pending, ends = set(owner), {}
    while pending:
        ready = sorted(t for t in pending if deps[t] <= ends.keys())
        if not ready: raise ValueError('paid official Task DAG contains cycle')
        for t in ready:
            release = max((ends[p] + (raw['task_cross_core_wait_cycles'] if owner[p] != owner[t] else 0)
                           for p in preds[t]), default=0)
            core = ends[previous[t]] + raw['task_same_core_wait_cycles'] if t in previous else 0
            ends[t] = max(release, core) + raw['step3_by_task'][str(t)]['local_makespan']
            pending.remove(t)
    return max(ends.values(), default=0)


def inspect(ir, embedded, expected_plan=None):
    record = read_json(embedded['record_path'])
    assert record['status'] == embedded['status'] == 'success'
    assert record['metrics'] == embedded['metrics']
    plan = read_json(record['plan_path']); validate_plan(ir, plan)
    if expected_plan:
        intended = read_json(expected_plan)
        assert intended == plan and list(intended['node_to_subgraph']) == list(plan['node_to_subgraph'])
    with gzip.open(record['result_path'], 'rt') as stream: raw = json.load(stream)
    assert_trace(ir, plan, raw, record)
    for key in ('makespan', 'memory_peak_by_core', 'data_movement_bytes'):
        assert raw[key] == record['metrics'][key]
    proxy = score_plan(ir, plan)
    return dict(makespan=raw['makespan'], proxy=proxy['proxy'], task_count=len(set(plan['node_to_subgraph'].values())),
                added_copy=raw['data_movement_bytes']['added_copy_bytes'], spill_copy=raw['data_movement_bytes']['spill_added_copy_bytes'],
                local_task_replay=local_duration_replay(plan, raw), memory_peak_by_core=raw['memory_peak_by_core'],
                memory_l1=max(v['L1'] for v in raw['memory_peak_by_core'].values()),
                memory_ub=max(v['UB'] for v in raw['memory_peak_by_core'].values()),
                ddr_max_active=max((e['active_count'] for e in raw['ddr_contention_log']), default=0),
                estimated_copy=proxy['components']['estimated_copy_bytes'], record_path=record['record_path'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=Path(__file__).resolve().parent/'P1多尺度联合优化_20260926/run_v1')
    args = parser.parse_args(); rows, parents, methods, checked = [], {}, [], 0
    for path in sorted((args.run/'arms').glob('*/*/summary.json')):
        arm = read_json(path); case, method = arm['case'], arm['method']; ir = GraphIR.from_path(DATA/(case+'.json'))
        if case not in parents: parents[case] = inspect(ir, arm['seed_record'])
        parent = parents[case]; candidates = []
        for call in arm['calls']:
            if call['record']['status'] != 'success': continue
            result = inspect(ir, call['record'], call['plan_path']); checked += 1
            assert abs(result['proxy'] - call['metadata']['proxy']) < 1e-6
            row = dict(case=case, method=method, name=call['name'], **result)
            row.update(parent_time=parent['makespan'], parent_proxy=parent['proxy'],
                       time_delta=result['makespan']-parent['makespan'], proxy_delta=result['proxy']-parent['proxy'],
                       copy_delta=result['added_copy']-parent['added_copy'], task_delta=result['task_count']-parent['task_count'],
                       l1_delta=result['memory_l1']-parent['memory_l1'], ub_delta=result['memory_ub']-parent['memory_ub'],
                       local_task_replay_delta=result['local_task_replay']-parent['local_task_replay'],
                       observed_minus_local_replay=result['makespan']-result['local_task_replay'])
            candidates.append(row); rows.append(row)
        if candidates:
            best = min(candidates, key=lambda r: (r['makespan'], r['added_copy']))
            methods.append(dict(case=case, method=method, candidate_best_name=best['name'],
                                candidate_best_time=best['makespan'], parent_time=parent['makespan'], time_delta=best['time_delta'],
                                candidate_best_proxy=best['proxy'], parent_proxy=parent['proxy'], proxy_delta=best['proxy_delta'],
                                copy_delta=best['copy_delta'], task_delta=best['task_delta'],
                                l1_delta=best['l1_delta'], ub_delta=best['ub_delta']))
    pairs, rankings = [], []
    for case, parent in parents.items():
        group = [r for r in rows if r['case'] == case]
        for a, b in itertools.combinations(group, 2):
            dp, dt = a['proxy']-b['proxy'], a['makespan']-b['makespan']
            relation = 'proxy_tie' if abs(dp) < 1e-6 else 'actual_tie' if dt == 0 else 'inverted' if dp*dt < 0 else 'concordant'
            pairs.append(dict(case=case, a=a['name'], b=b['name'], proxy_delta=dp, actual_delta=dt, relation=relation))
        subset = [p for p in pairs if p['case'] == case]
        rankings.append(dict(case=case, parent=parent, proxy_order=[r['name'] for r in sorted(group,key=lambda r:r['proxy'])],
                             official_order=[r['name'] for r in sorted(group,key=lambda r:r['makespan'])],
                             concordant=sum(p['relation']=='concordant' for p in subset), inverted=sum(p['relation']=='inverted' for p in subset),
                             ties=sum('tie' in p['relation'] for p in subset),
                             parent_vs_candidate_proxy_sign_errors=sum(r['proxy_delta']*r['time_delta'] < 0 for r in group)))
    output = args.run/'离线诊断'; output.mkdir(exist_ok=True)
    write_csv(output/'候选完整对照.csv', rows); write_csv(output/'方法实际候选最优.csv', methods); write_csv(output/'代理实际成对排序.csv', pairs)
    atomic_json(output/'诊断.json', dict(verified_official_candidates=checked, verified_parents=len(parents), rankings=rankings,
        limitation='Paired comparisons are correlated observations on three graphs, not general error rates. Local-duration replay is diagnostic, not an official counterfactual or a claimed DDR-only attribution.'))
    print(json.dumps(dict(verified_official_candidates=checked, methods=methods, rankings=rankings),ensure_ascii=False,indent=2))


if __name__ == '__main__': main()
