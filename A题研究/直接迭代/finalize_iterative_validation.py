"""Audit the frozen cold pilot, replay improvements, and export cumulative scores.

Run from the repository root. Existing run directories are read-only. Replays
require a new output directory; this script never changes the frozen solvers.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import math
import shutil
import statistics
from common_run import *

ROOT = R.parent
DELIVERY = R/'直接迭代/闭环验证_20260925'
ROUND6 = R/'直接迭代/第六轮成果'
PRIOR = R/'直接迭代/联合推进成果_20260925'
RUNS = ('iterative_holdout_b8_v1', 'iterative_holdout_b12_v1', 'reserved_cold_b12_v1')


def relative(path):
    return str(Path(path).relative_to(ROOT))


def compact(value):
    if isinstance(value, dict):
        return {k: compact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [compact(v) for v in value]
    if isinstance(value, str) and value.startswith(str(ROOT)+'/'):
        return relative(value)
    return value


def audit_cold():
    run = ROOT/'my_runs'/RUNS[2]
    s = read_json(run/'summary.json')
    protocol = read_json(run/'protocol.json')
    assert s['completed'] and len(s['rows']) == 18 and not s['failures']
    paired, trials, certificates = [], [], []
    for job in protocol['jobs']:
        case, n = job['case'], job['cores']
        arms = {x['method']: x for x in s['rows'] if (x['case'], x['cores']) == (case, n)}
        assert set(arms) == {'original', 'single', 'iterative'}
        parent = run/case/f'n{n}'
        a = read_json(parent/'single/prefix_certificate.json')
        b = read_json(parent/'iterative/prefix_certificate.json')
        equal = a == b
        certificates.append(dict(case=case, cores=n, identical=equal, single=a, iterative=b))
        original, single, iterative = [arms[k]['after'] for k in ('original', 'single', 'iterative')]
        paired.append(dict(case=case, cores=n, original=original, single=single, iterative=iterative,
            reduction_vs_single_pct=100*(single-iterative)/single,
            reduction_vs_original_pct=100*(original-iterative)/original,
            reference_speedup_delta=job['original_singlecore']*(1/iterative-1/single),
            identical_prefix=equal))
        for method, row in arms.items():
            assert row['logical_calls'] <= protocol['budget']
            result = read_json(parent/method/'summary.json')
            for i, ev in enumerate(result['evaluations']):
                rec = ev['record']
                trials.append(dict(case=case, cores=n, method=method, index=i+1,
                    name=ev['name'], phase=ev['phase'], generation=ev.get('generation'),
                    accepted=ev.get('accepted'), status=rec['status'], cache_hit=rec['cache_hit'],
                    makespan=score(rec)[0] if rec['status']=='success' else None,
                    added_copy=score(rec)[1] if rec['status']=='success' else None,
                    plan_sha256=rec['hashes']['plan_sha256']))
    mean_single = statistics.mean(x['reduction_vs_single_pct'] for x in paired)
    mean_original = statistics.mean(x['reduction_vs_original_pct'] for x in paired)
    winning = {x['case'] for x in paired if x['iterative'] < x['single']}
    checks = dict(at_least_two_winning_graphs=len(winning)>=2,
        positive_mean_vs_single=mean_single>0, nonnegative_mean_vs_original=mean_original>=0,
        regression_vs_original_within_one_pct=min(x['reduction_vs_original_pct'] for x in paired)>=-1,
        identical_prefixes=all(x['identical_prefix'] for x in paired),
        feasible_without_errors_or_timeouts=all(x['status']=='success' and not x['errors'] and not x['timeouts'] for x in s['rows']))
    gate = dict(checks=checks, expand_budget_8_16=all(checks.values()),
        wins=len(winning), ties=sum(x['iterative']==x['single'] for x in paired),
        losses=sum(x['iterative']>x['single'] for x in paired),
        mean_paired_reduction_pct=mean_single,
        mean_reference_speedup_delta=statistics.mean(x['reference_speedup_delta'] for x in paired),
        actual_new_calls=s['actual_new_calls'], logical_calls=s['logical_calls'],
        elapsed_seconds=s['elapsed_seconds'], scope=protocol['scope'])
    out = DELIVERY/'从头预算12'
    write_csv(out/'逐配置对照.csv', paired)
    write_csv(out/'全部候选记录.csv', trials)
    atomic_json(out/'共同前缀核验.json', certificates)
    atomic_json(out/'晋级判定.json', gate)
    atomic_json(out/'执行汇总.json', compact(s))
    return gate


def replay_one(item, replay):
    (case, problem, n), source, rec = item
    result = run_candidate(case, problem, n, read_json(rec['plan_path']), replay/f'{case}_p{problem}_n{n}', timeout=60)
    assert result['status']=='success' and not result['cache_hit'] and score(result)==score(rec), (case, n, result)
    return item, result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--replay-out', type=Path, required=True)
    args = p.parse_args()
    args.replay_out.mkdir(parents=True, exist_ok=False)
    gate = audit_cold()
    rows = list(csv.DictReader((ROUND6/'全部成绩.csv').open(encoding='utf-8-sig')))
    assert len(rows)==1500
    base = {(x['case'], int(x['problem']), int(x['cores'])): x for x in rows}
    current = {k: dict(case=k[0], problem=k[1], cores=k[2], makespan=int(v['after']),
        added_copy=int(v['after_copy_bytes']), original_singlecore=int(v['original_singlecore']),
        source='round6', plan=relative(ROUND6/v['plan'])) for k,v in base.items()}
    for row in csv.DictReader((PRIOR/'精选改善.csv').open(encoding='utf-8-sig')):
        k = row['case'], int(row['problem']), int(row['cores'])
        current[k].update(makespan=int(row['after']), added_copy=int(row['after_copy']),
                         source='previous_joint_delivery', plan=relative(PRIOR/row['plan']))
    selected = {}
    for name in RUNS:
        s = read_json(ROOT/'my_runs'/name/'summary.json')
        for row in s['rows']:
            rec = read_json(row['summary'])['best_record']
            k = row['case'], 1, row['cores']
            if score(rec) < (current[k]['makespan'], current[k]['added_copy']):
                if k not in selected or score(rec) < score(selected[k][1]):
                    selected[k] = relative(row['summary']), rec
    inputs = [(k, source, rec) for k,(source,rec) in sorted(selected.items())]
    atomic_json(args.replay_out/'inputs.json', compact([dict(key=k, source=s, record=r) for k,s,r in inputs]))
    verified = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for (k, source, rec), replay in pool.map(lambda x: replay_one(x, args.replay_out), inputs):
            case, problem, n = k
            name = f'{case}_p{problem}_n{n}'
            dest = DELIVERY/'精选方案'/f'{name}.json'
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(replay['plan_path'], dest)
            official = DELIVERY/'官方复评'/name
            official.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(replay['result_path'], official/'official_result.json.gz')
            exported = compact(replay)
            exported['plan_path'] = relative(dest)
            exported['result_path'] = relative(official/'official_result.json.gz')
            atomic_json(official/'record.json', exported)
            before = current[k]['makespan']
            verified.append(dict(case=case, problem=problem, cores=n, before=before,
                after=score(replay)[0], before_copy=current[k]['added_copy'], after_copy=score(replay)[1],
                reduction_pct=100*(before-score(replay)[0])/before, source=source,
                plan=relative(dest), official_record=relative(official/'record.json'),
                replay_status=replay['status'], new_call=not replay['cache_hit']))
            current[k].update(makespan=score(replay)[0], added_copy=score(replay)[1], source=source, plan=relative(dest))
            print(json.dumps(verified[-1], ensure_ascii=False), flush=True)
    write_csv(DELIVERY/'新增精选改善.csv', verified)
    write_csv(DELIVERY/'累计1500配置成绩.csv', [dict(x, speedup=x['original_singlecore']/x['makespan']) for _,x in sorted(current.items())])
    means = []
    for problem in (1,2,3):
        for n in range(1,6):
            subset = [x for k,x in current.items() if k[1:]==(problem,n)]
            assert len(subset)==100
            old = [x for k,x in base.items() if k[1:]==(problem,n)]
            means.append(dict(problem=problem, cores=n, count=100,
                round6_mean_speedup=statistics.mean(int(x['original_singlecore'])/int(x['after']) for x in old),
                cumulative_mean_speedup=statistics.mean(x['original_singlecore']/x['makespan'] for x in subset)))
    write_csv(DELIVERY/'累计平均加速比.csv', means)
    bounds = []
    for x in sorted((x for k,x in current.items() if k[1:]==(1,5)), key=lambda x:x['original_singlecore']/x['makespan'])[:8]:
        ir = GraphIR.from_path(DATA/(x['case']+'.json'))
        plan = read_json(ROOT/x['plan'])
        core = {str(task): c for c,tasks in enumerate(plan['core_schedules']) for task in tasks}
        work = [[0,0] for _ in range(5)]
        for i in ir.compute_ids:
            op = ir.ops[i]
            if op['pipe'] in ('PIPE_M','PIPE_V'):
                work[core[str(plan['node_to_subgraph'][str(i)])]][op['pipe']=='PIPE_V'] += op['cycles']
        total = max(sum(w[j] for w in work) for j in (0,1))
        global_lb = math.ceil(total/5)
        fixed_lb = max(max(w) for w in work)
        bounds.append(dict(case=x['case'], makespan=x['makespan'], speedup=x['original_singlecore']/x['makespan'],
            global_mv_work_lower_bound=global_lb, fixed_assignment_mv_work_lower_bound=fixed_lb,
            fixed_work_bound_fraction=fixed_lb/x['makespan'],
            max_to_ideal_work_ratio=fixed_lb/(total/5) if total else None,
            core_mv_cycles=json.dumps(work), scope='necessary M/V work bounds only; excludes copies/dependencies; gap is not attainable gain'))
    write_csv(DELIVERY/'P1低加速图工作量诊断.csv', bounds)
    frozen = read_json(DELIVERY/'从头预登记.json')
    for rel, digest in {**frozen['python_sha256'], **frozen['official_sha256']}.items():
        assert hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()==digest, rel
    manifest = dict(scope='cumulative selected plans, NOT uniform-budget full-run score',
        cold_gate=gate, selected_replayed=len(verified),
        new_official_calls_this_stage=sum(read_json(ROOT/'my_runs'/name/'summary.json')['actual_new_calls'] for name in RUNS)+len(verified),
        previous_stage_calls=85, tests_passed=25, official_files_unchanged=len(frozen['official_sha256']),
        means=means, selected=verified,
        sha256={relative(f):hashlib.sha256(f.read_bytes()).hexdigest() for folder in ('精选方案','官方复评') for f in (DELIVERY/folder).rglob('*') if f.is_file()})
    atomic_json(DELIVERY/'manifest.json', manifest)
    atomic_json(args.replay_out/'summary.json', dict(completed=True, fresh_replays=len(verified), all_matched=True))
    print(json.dumps(dict(cold_gate=gate, means=means, selected_replayed=len(verified)), ensure_ascii=False))


if __name__ == '__main__':
    main()
