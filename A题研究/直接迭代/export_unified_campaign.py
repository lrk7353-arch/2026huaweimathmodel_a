"""Export complete cold-search results, official curves and portable evidence.

This never blends historical best solutions into the uniform solver's score.
P1/P2 formal curves anchor one core at 1; raw one-core solver times remain visible.
P3 hardware ratios compare independently optimized P2/P3 at equal core counts.
"""
import argparse
from collections import Counter
import csv
import gzip
import json
from pathlib import Path
import shutil
import statistics

from common_run import DATA, GraphIR, atomic_json, read_json, write_csv
from audit_unified_results import sha, validate_record


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def curve_rows(rows):
    table = {(r['case'], r['problem'], r['cores']): r for r in rows}
    curves = []
    for problem in (1, 2, 3):
        for n in range(1, 6):
            group = [r for r in rows if r['problem'] == problem and r['cores'] == n]
            if not group:
                continue
            curve = dict(problem=problem, cores=n, graphs=len(group),
                mean_original_singlecore_speedup=statistics.mean(r['speedup'] for r in group),
                mean_makespan=statistics.mean(r['makespan'] for r in group),
                mean_added_copy=statistics.mean(r['added_copy'] for r in group),
                formal_P1_P2_speedup=(1.0 if n == 1 else statistics.mean(r['speedup'] for r in group)) if problem in (1, 2) else '',
                mean_P3_same_core_noL2_over_L2='', mean_P3_byte_hit_rate='')
            if problem == 3:
                ratios = [table[r['case'], 2, n]['makespan']/r['makespan'] for r in group if (r['case'], 2, n) in table]
                if ratios:
                    assert len(ratios) == len(group)
                    curve['mean_P3_same_core_noL2_over_L2'] = statistics.mean(ratios)
                curve['mean_P3_byte_hit_rate'] = statistics.mean(r['cache_byte_hit_rate'] for r in group)
            curves.append(curve)
    return curves


def export(run, out, final=False, raw=True):
    run, out = run.resolve(), out.resolve()
    manifest, completion = read_json(run/'manifest.json'), read_json(run/'completion.json')
    assert not completion['errors'] and completion['completed'] == completion['total']
    assert completion['valid_slots'] == completion['total'], 'invalid slots must be resolved before final delivery'
    assert manifest['variants'] == ['v2'], 'export a single frozen uniform version'
    if final:
        assert manifest['cases'] == [f'case_{i:03d}' for i in range(1, 101)]
        assert set(manifest['problems']) == {1, 2, 3} and set(manifest['cores']) == set(range(1, 6))
        assert manifest['singlecore_baselines'] and completion['total'] == 1600
    out.mkdir(parents=True, exist_ok=True)
    baseline, summaries = {}, {}
    for path in sorted((run/'slots').glob('case_*/p*_n*/*/summary.json')):
        s = read_json(path)
        key = s['case'], s['problem'], s['num_cores']
        assert key not in summaries
        summaries[key] = s
        if s['problem'] == 0:
            validate_record(s['best_record'], raw=raw)
            baseline[s['case']] = s['best_record']['metrics']['makespan']
    expected = {(c, p, n) for c in manifest['cases'] for p in manifest['problems'] for n in manifest['cores']}
    assert {k for k in summaries if k[1]} == expected, 'missing/duplicate cells'
    assert set(baseline) == set(manifest['cases']), 'official single-core denominator missing'
    rows, plans, evidence, attempts = [], {}, [], []
    for case in manifest['cases']:
        ir = GraphIR.from_path(DATA/(case+'.json'))
        assert sha(DATA/(case+'.json')) == manifest['inputs'][case]
        for p in manifest['problems']:
            for n in manifest['cores']:
                s = summaries[case, p, n]
                assert s['mode'] == 'cold' and not s.get('upstream_searches')
                assert s['budget'] == manifest['budget'] and s['seed'] == manifest['seed']
                assert s['logical_calls'] <= s['budget']
                rec = s['best_record']
                validate_record(rec, ir, raw=raw)
                metrics = rec['metrics']
                rel = Path('方案')/f'p{p}'/f'n{n}'/(case+'_multicore_res.json')
                target = out/rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(rec['plan_path'], target)
                assert sha(target) == rec['hashes']['plan_sha256']
                plans[str(rel)] = read_json(target)
                row = dict(case=case, problem=p, cores=n, makespan=metrics['makespan'],
                    original_singlecore=baseline[case], speedup=baseline[case]/metrics['makespan'],
                    added_copy=metrics['data_movement_bytes']['added_copy_bytes'],
                    cache_byte_hit_rate=metrics.get('cache_stats', {}).get('hit_rate', ''),
                    selected_name=s['best']['name'], calls=s['logical_calls'], new_calls=s['new_calls'],
                    elapsed_seconds=s['elapsed_seconds'], generation_seconds=s['generation_seconds'],
                    failures=sum(x['record']['status'] != 'success' for x in s['evaluations']),
                    timeouts=sum(x['record']['status'] == 'timeout' for x in s['evaluations']),
                    mode=s['mode'], plan=str(rel), plan_sha256=rec['hashes']['plan_sha256'],
                    result_sha256=rec['result_sha256'], graph_sha256=rec['hashes']['graph_sha256'],
                    stop_reason=s['stop_reason'])
                rows.append(row)
                evidence.append(dict(case=case, problem=p, cores=n, metrics=metrics, hashes=rec['hashes'],
                    plan=str(rel), original_record_path=rec['record_path'], result_sha256=rec['result_sha256'],
                    original_result_path=rec['result_path']))
                for index, a in enumerate(s['evaluations']):
                    r = a['record']
                    attempts.append(dict(case=case, problem=p, cores=n, call=index+1, name=a['name'],
                        status=r['status'], makespan=r['metrics'].get('makespan'), seconds=r['elapsed_seconds'],
                        cache_hit=r['cache_hit'], accepted=a['accepted'], error=r.get('error'),
                        plan_sha256=r['hashes'].get('plan_sha256'), record_path=r['record_path']))
        print(json.dumps(dict(exported_case=case, cells=len(rows))), flush=True)
    write_csv(out/'统一算法逐配置成绩.csv', rows)
    write_csv(out/'全部调用账本.csv', attempts)
    curves = curve_rows(rows)
    write_csv(out/'原题平均曲线.csv', curves)
    for name, obj in [('方案集.json.gz', plans), ('官方证据摘要.json.gz', evidence)]:
        # Deterministic gzip output makes publication hashes stable.
        with (out/name).open('wb') as f, gzip.GzipFile(fileobj=f, mode='wb', mtime=0) as stream:
            stream.write(json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode())
    atomic_json(out/'冻结运行配置.json', manifest)
    atomic_json(out/'原单核官方基线.json', {c:dict(makespan=t, record=summaries[c,0,1]['best_record']) for c,t in baseline.items()})
    audit = dict(expected_cells=len(expected), valid_cells=len(rows), singlecore_baselines=len(baseline),
        source_commit=manifest['code_commit'], source_config_sha256=manifest['config'],
        official_calls=sum(s['new_calls'] for s in summaries.values()),
        target_calls=sum(r['new_calls'] for r in rows), baseline_calls=sum(s['new_calls'] for k,s in summaries.items() if k[1] == 0),
        wall_seconds=completion['elapsed_seconds'], failures=sum(r['failures'] for r in rows),
        timeouts=sum(r['timeouts'] for r in rows), status_counts=dict(Counter(r['status'] for r in attempts)),
        max_slot_seconds=max(r['elapsed_seconds'] for r in rows),
        slots_over_requested_seconds=sum(r['elapsed_seconds'] > manifest['seconds'] for r in rows),
        all_best_plan_hashes_checked=True, all_input_and_official_hashes_checked=True,
        raw_official_metrics_checked=raw, uniform_mode='cold',
        plan_bundle_sha256=sha(out/'方案集.json.gz'), evidence_bundle_sha256=sha(out/'官方证据摘要.json.gz'),
        interpretation='No historical incumbents. P3 same-core ratio compares independent P2/P3 searches; not a same-plan cache ablation. No official scalar overall score exists.')
    atomic_json(out/'交付核验.json', audit)
    text = ['# 统一算法冻结全量结果', '',
            f'有效配置 {len(rows)}/{len(expected)}；另有 {len(baseline)} 个原单核官方基线。所有方案均从头求解。', '',
            '|问题|核数|原单核/当前时间的均值|P3同核无L2/有L2均值|', '|---|---:|---:|---:|']
    for c in curves:
        ratio = c['mean_P3_same_core_noL2_over_L2']
        text.append(f"|P{c['problem']}|{c['cores']}|{c['mean_original_singlecore_speedup']:.8f}|{ratio if ratio != '' else '—'}|")
    text += ['', 'P1/P2原题曲线的单核锚点为1；上表保留实际单核求解器时间之比，避免隐藏单核额外开销。',
        'P3的同核比较来自分别优化的P2和P3方案，包含映射/次序变化，不能当作固定方案下L2的纯收益。',
        f"官方调用 {audit['official_calls']} 次（目标配置 {audit['target_calls']}，基线 {audit['baseline_calls']}）；失败调用 {audit['failures']}，其中超时 {audit['timeouts']}。", 
        f"整批耗时 {audit['wall_seconds']:.1f} 秒；超过请求软时间预算的配置 {audit['slots_over_requested_seconds']} 个，最长 {audit['max_slot_seconds']:.2f} 秒。", '',
        '方案集.json.gz包含相对路径到完整官方方案的映射；解压JSON后按键写出即可恢复方案目录。官方证据摘要保留输入、官方程序及方案哈希和完整指标；大体积原始轨迹在本地原运行目录。',
        '全量本身使用公开的100图池，是交付评测，不是未见测试集上的泛化证明。累计最佳方案库另行列出，不混入此成绩。没有原题明确的三题总分公式，不制造综合得分。']
    (out/'全量结果说明.md').write_text('\n'.join(text)+'\n', encoding='utf-8')
    return audit


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--final', action='store_true')
    p.add_argument('--skip-raw', action='store_true', help='development only; not final evidence audit')
    a = p.parse_args()
    if a.final and a.skip_raw:
        p.error('final export requires checking raw official results')
    print(json.dumps(export(a.run, a.out, a.final, not a.skip_raw), ensure_ascii=False))
