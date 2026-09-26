"""Build a separate complete 1500-plan package with three verified P1 updates.

Reads the base package directly from Git, never modifies it, never calls an
official evaluator, and preserves every unchanged plan payload byte for byte.
"""
import argparse
import copy
import csv
from functools import lru_cache
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import time

from common_run import DATA, GraphIR, validate_plan


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DEFAULT_BASE_REF = 'origin/codex/continuous-search-delivery'
BASE_DIR = 'A题研究/直接迭代/持续联合冲刺_20260926/最终精选1500'
TARGETS = {'case_047', 'case_075', 'case_085'}
CSV_FIELDS = ['case', 'problem', 'cores', 'makespan', 'added_copy',
              'original_singlecore', 'speedup', 'source', 'source_plan',
              'plan', 'verification']


def git_blob(ref, name):
    return subprocess.check_output(['git', 'show', f'{ref}:{BASE_DIR}/{name}'], cwd=REPO)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def plan_from_bytes(payload):
    return json.loads(payload, object_pairs_hook=unique_object)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def exact_signature(plan):
    """Ordering of op keys can influence official compilation; keep it."""
    return tuple(plan['node_to_subgraph'].items()), tuple(tuple(s) for s in plan['core_schedules'])


def assert_same_plan(first, second, label):
    if exact_signature(first) != exact_signature(second):
        raise ValueError(f'Ordered plan mismatch: {label}')


def score(record):
    metrics = record['metrics']
    return metrics['makespan'], metrics['data_movement_bytes']['added_copy_bytes']


def deterministic_metrics(record):
    metrics = copy.deepcopy(record['metrics'])
    # This is host process RSS, not simulated L1/UB.  The simulated memory
    # metrics remain in the comparison, as do every other deterministic field.
    metrics.pop('peak_memory_bytes', None)
    return metrics


def checked_record(record, case):
    if record.get('status') != 'success' or record.get('problem') != 1:
        raise ValueError('Verified record must be a successful P1 evaluation')
    if record['metrics']['num_cores'] != 5 or Path(record['graph_path']).stem != case:
        raise ValueError('Verified record graph/core mismatch')
    if record.get('cache_hit'):
        raise ValueError('Independent replay must not be a cache hit')
    payload = Path(record['plan_path']).read_bytes()
    return payload, plan_from_bytes(payload)


def compact_evidence(record):
    return dict(attempt_id=record.get('attempt_id'), status=record['status'],
                problem=record['problem'], metrics=record['metrics'],
                graph_path=record['graph_path'], plan_path=record['plan_path'],
                source_record_path=record.get('record_path'),
                independent_cache_hit=record.get('cache_hit'),
                elapsed_seconds=record.get('elapsed_seconds'))


def candidate(case, source, record, exported, original_record_path=None):
    payload, plan = checked_record(record, case)
    export_plan = plan_from_bytes(Path(exported).read_bytes())
    assert_same_plan(plan, export_plan, f'{source} export/replay')
    evidence = dict(replay=compact_evidence(record), ordered_plan_matches=True)
    if original_record_path:
        original = read_json(original_record_path)
        _, original_plan = checked_record(original, case)
        assert_same_plan(plan, original_plan, f'{source} original/replay')
        if original['attempt_id'] == record['attempt_id']:
            raise ValueError('Original and independent replay are the same attempt')
        if deterministic_metrics(original) != deterministic_metrics(record):
            raise ValueError(f'Independent deterministic metrics differ: {source}/{case}')
        evidence.update(original=compact_evidence(original), independent_attempts=True,
                        deterministic_metrics_match=True)
    return dict(case=case, source=source, makespan=score(record)[0],
                added_copy=score(record)[1], payload=payload, plan=plan,
                source_plan=str(exported), evidence=evidence)


def verified_candidates(root, base_rows, base_payloads):
    relay = HERE/'P1接力实验_20260926'
    all_candidates = []
    # The earlier relay independently replayed the base selected plans as well
    # as three regional seeds.  Keep the regional seeds if they are Pareto-useful.
    for case in sorted(TARGETS):
        for kind in ('selected', 'region'):
            path = relay/'run_v1'/'seeds'/f'{case}_{kind}'/'seed.json'
            seed = read_json(path)
            if not seed.get('complete') or not seed.get('verified') or seed.get('error'):
                raise ValueError(f'Seed was not independently verified: {path}')
            record = seed['record']
            item = candidate(case, 'base_selected' if kind == 'selected' else 'relay_region_seed',
                             record, seed['seed']['plan_path'])
            expected = (seed['seed']['expected_makespan'], seed['seed']['expected_added_copy_bytes'])
            if score(record) != expected:
                raise ValueError('Seed replay differs from recorded expected values')
            if kind == 'selected':
                base = base_rows[case, 1, 5]
                if (int(base['makespan']), int(base['added_copy'])) != expected:
                    raise ValueError('Selected seed is not the requested Git base')
                assert_same_plan(item['plan'], plan_from_bytes(base_payloads[base['plan']]),
                                 'Git base/independent selected seed')
                item['payload'] = base_payloads[base['plan']]
                item['source_plan'] = base['plan']
            item['evidence']['verification_manifest'] = str(path)
            all_candidates.append(item)

    summary_path = relay/'独立复评与新增方案'/'summary.json'
    summary = read_json(summary_path)
    if not summary.get('complete'):
        raise ValueError('Relay acceptance summary is incomplete')
    for row in summary['rows']:
        if row['case'] not in TARGETS or not row.get('verified'):
            continue
        verification = read_json(row['verification_path'])
        if not verification.get('accepted') or not verification.get('deterministic_metrics_match'):
            raise ValueError('Relay acceptance verification was not successful')
        item = candidate(row['case'], 'relay_verified', verification['replay_record'], row['plan_path'],
                         verification['candidate']['record_path'])
        if (item['makespan'], item['added_copy']) != (row['makespan'], row['added_copy_bytes']):
            raise ValueError('Relay summary/replay metrics mismatch')
        item['evidence']['verification_manifest'] = row['verification_path']
        all_candidates.append(item)

    for run in ('run_v1', 'run_v2'):
        path = root/run/'acceptance'/'summary.json'
        summary = read_json(path)
        if not summary.get('complete'):
            raise ValueError(f'Incomplete acceptance: {run}')
        for row in summary['rows']:
            if row['case'] not in TARGETS or not row.get('accepted'):
                continue
            item = candidate(row['case'], run+'_'+row['method'], row['replay_record'], row['plan_path'],
                             row['source_record_path'])
            if (item['makespan'], item['added_copy']) != tuple(row['candidate_score']):
                raise ValueError('Acceptance summary/replay metrics mismatch')
            item['evidence']['verification_manifest'] = str(path)
            all_candidates.append(item)
    wanted = {'case_047': 'run_v2_cpsat_warm', 'case_075': 'relay_verified',
              'case_085': 'run_v2_cpsat_intact'}
    selected = {}
    for case, source in wanted.items():
        matching = [item for item in all_candidates if item['case'] == case and item['source'] == source]
        if len(matching) != 1:
            raise ValueError(f'Expected exactly one requested replacement: {case}/{source}')
        selected[case] = matching[0]
    return all_candidates, selected


def atomic_text(path, value):
    temp = path.with_name(path.name+'.tmp')
    temp.write_text(value, encoding='utf-8')
    os.replace(temp, path)


def write_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def write_csv(path, rows, fields=None):
    fields = fields or list(dict.fromkeys(k for row in rows for k in row))
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(path, '\ufeff'+stream.getvalue())


def make_stats(rows):
    result = {}
    for problem in (1, 2, 3):
        result[f'P{problem}'] = {}
        for cores in range(1, 6):
            group = [r for r in rows if int(r['problem']) == problem and int(r['cores']) == cores]
            if len(group) != 100:
                raise ValueError('Each problem/core group must contain 100 graphs')
            speedups = [int(r['original_singlecore'])/int(r['makespan']) for r in group]
            result[f'P{problem}'][str(cores)] = dict(
                graphs=100, mean_speedup=math.fsum(speedups)/100,
                makespan_sum=sum(int(r['makespan']) for r in group),
                added_copy_sum=sum(int(r['added_copy']) for r in group))
    return result


def build(root, output, base_ref):
    started = time.perf_counter()
    # A delivery is always written into a new directory.  In particular,
    # --output must not overwrite an old library or an existing delivery.
    output.mkdir(parents=True, exist_ok=False)
    old_csv = git_blob(base_ref, '累计1500配置成绩.csv').decode('utf-8-sig')
    old_rows = list(csv.DictReader(io.StringIO(old_csv)))
    indexed = {(r['case'], int(r['problem']), int(r['cores'])): r for r in old_rows}
    expected = {(f'case_{case:03}', problem, cores) for case in range(1, 101)
                for problem in (1, 2, 3) for cores in range(1, 6)}
    if len(old_rows) != 1500 or set(indexed) != expected:
        raise ValueError('Base CSV does not uniquely cover 100 graphs x 3 scenes x 5 core counts')
    by_name = {r['plan']: r for r in old_rows}
    if len(by_name) != 1500:
        raise ValueError('Base CSV contains duplicate archive paths')
    for row in old_rows:
        recomputed = int(row['original_singlecore'])/int(row['makespan'])
        if not math.isclose(recomputed, float(row['speedup']), rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError('Base speedup disagrees with original single-core/makespan')
    archive_bytes = git_blob(base_ref, 'selected_plans.tar.gz')
    target_names = {indexed[case, 1, 5]['plan'] for case in TARGETS}
    base_payloads = {}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:gz') as archive:
        for member in archive:
            if member.name in target_names:
                base_payloads[member.name] = archive.extractfile(member).read()
    if set(base_payloads) != target_names:
        raise ValueError('Base archive is missing a requested replacement plan')
    candidates, selected = verified_candidates(root, indexed, base_payloads)

    @lru_cache(maxsize=2)
    def graph(case):
        return GraphIR.from_path(DATA/(case+'.json'))

    for item in candidates:
        validate_plan(graph(item['case']), item['plan'])
        if len(item['plan']['core_schedules']) != 5:
            raise ValueError('Candidate does not have five core schedules')
    replacements = {indexed[case, 1, 5]['plan']: item['payload'] for case, item in selected.items()}
    destination = output/'selected_plans.tar.gz'
    temporary = output/'selected_plans.tar.gz.tmp'
    covered = set()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:gz') as original, \
            tarfile.open(temporary, mode='w:gz', compresslevel=6) as result:
        for member in original:
            if not member.isfile() or member.name not in by_name:
                raise ValueError(f'Unexpected non-plan archive member: {member.name}')
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or '..' in relative.parts or member.name in covered:
                raise ValueError('Archive path is unsafe or duplicated')
            covered.add(member.name)
            old_payload = original.extractfile(member).read()
            payload = replacements.get(member.name, old_payload)
            row = by_name[member.name]
            plan = plan_from_bytes(payload)
            validate_plan(graph(row['case']), plan)
            if len(plan['core_schedules']) != int(row['cores']):
                raise ValueError('Archive plan has incorrect core count')
            info = copy.copy(member)
            info.size = len(payload)
            result.addfile(info, io.BytesIO(payload))
    if covered != set(by_name):
        raise ValueError('Archive coverage differs from the complete score table')

    # Reopen the finished archive and directly compare decoded payload bytes.
    # A compression difference is irrelevant; every inherited JSON is exact.
    unchanged, changed = 0, []
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:gz') as original, \
            tarfile.open(temporary, mode='r:gz') as result:
        old_iter, new_iter = iter(original), iter(result)
        while True:
            old = next(old_iter, None)
            new = next(new_iter, None)
            if old is None or new is None:
                if old is not None or new is not None:
                    raise ValueError('Written archive member count changed')
                break
            if old.name != new.name:
                raise ValueError('Written archive path/order changed')
            before, after = original.extractfile(old).read(), result.extractfile(new).read()
            if before == after:
                unchanged += 1
            else:
                changed.append(old.name)
                if old.name not in replacements or after != replacements[old.name]:
                    raise ValueError('Unexpected plan payload change')
    if unchanged != 1497 or set(changed) != set(replacements):
        raise ValueError('Expected exactly three updates and 1497 byte-identical plans')
    os.replace(temporary, destination)

    new_rows, changes = [], []
    for old in old_rows:
        row = {field: old.get(field, '') for field in CSV_FIELDS}
        case = old['case']
        if (case, int(old['problem']), int(old['cores'])) in {(c, 1, 5) for c in TARGETS}:
            item = selected[case]
            row.update(makespan=item['makespan'], added_copy=item['added_copy'],
                       speedup=int(old['original_singlecore'])/item['makespan'],
                       source=item['source'], source_plan=item['source_plan'],
                       verification='independent replay matched ordered plan and deterministic metrics')
            changes.append(dict(case=case, problem=1, cores=5,
                old_makespan=int(old['makespan']), new_makespan=item['makespan'],
                time_reduction_percent=100*(1-item['makespan']/int(old['makespan'])),
                old_added_copy=int(old['added_copy']), new_added_copy=item['added_copy'],
                added_copy_delta=item['added_copy']-int(old['added_copy']),
                source=item['source'], archive_plan=old['plan'],
                independent_replay_record=item['evidence']['replay']['source_record_path']))
        new_rows.append(row)
    write_csv(output/'累计1500配置成绩.csv', new_rows, CSV_FIELDS)
    write_csv(output/'三项变更.csv', changes)

    pareto_dir = output/'备选'
    pareto_dir.mkdir(exist_ok=True)
    audit, frontier = [], []
    for item in candidates:
        dominates = lambda other: (other['case'] == item['case']
            and other['makespan'] <= item['makespan'] and other['added_copy'] <= item['added_copy']
            and (other['makespan'] < item['makespan'] or other['added_copy'] < item['added_copy']))
        dominated_by = [other['source'] for other in candidates if dominates(other)]
        entry = dict(case=item['case'], source=item['source'], makespan=item['makespan'],
                     added_copy=item['added_copy'], independently_replayed=True,
                     nondominated=not dominated_by, dominated_by=';'.join(dominated_by),
                     selected_in_1500=item is selected[item['case']], source_plan=item['source_plan'])
        if not dominated_by:
            filename = f'{item["case"]}_{item["source"]}_p1_n5.json'
            (pareto_dir/filename).write_bytes(item['payload'])
            entry['alternative_plan'] = '备选/'+filename
            frontier.append(entry.copy())
        audit.append(entry)
    write_csv(pareto_dir/'时间_COPY非支配备选.csv', frontier)
    write_csv(pareto_dir/'全部已复评候选审计.csv', audit)
    evidence = [dict(case=item['case'], source=item['source'], **item['evidence']) for item in candidates]
    write_json(pareto_dir/'独立复评证据.json', evidence)

    previous, current = make_stats(old_rows), make_stats(new_rows)
    for problem in ('P2', 'P3'):
        if previous[problem] != current[problem]:
            raise ValueError(f'{problem} aggregate metrics changed unexpectedly')
    p1_before, p1_after = previous['P1']['5']['mean_speedup'], current['P1']['5']['mean_speedup']
    stats = dict(complete=True, scope='cumulative selected library; not a new cold-start algorithm or official total score',
                 base_ref=base_ref, base_path=BASE_DIR, plans=1500, unique_coverage=True,
                 structure_validated_plans=1500, graph_cache_loads=graph.cache_info().misses,
                 unchanged_payloads=unchanged, changed_payloads=len(changed), changed_paths=changed,
                 ordered_replay_plan_match=True, new_official_calls=0, official_1500_rerun=False,
                 candidate_audit_count=len(audit), nondominated_alternatives=len(frontier),
                 nondominated_additional_to_selected=sum(not r['selected_in_1500'] for r in frontier),
                 previous=previous, current=current,
                 p1_n5_mean_speedup_before=p1_before, p1_n5_mean_speedup_after=p1_after,
                 p1_n5_mean_speedup_relative_increase_percent=100*(p1_after/p1_before-1),
                 elapsed_seconds=time.perf_counter()-started)
    write_json(output/'统计与验证.json', stats)
    change_lines = '\n'.join(f'| {r["case"]} | {r["old_makespan"]} → {r["new_makespan"]} | '
                             f'{r["time_reduction_percent"]:.6f}% | {r["added_copy_delta"]:+d} |'
                             for r in changes)
    group_lines = '\n'.join(f'| {p} | {previous[p]["5"]["mean_speedup"]:.10f} | '
                            f'{current[p]["5"]["mean_speedup"]:.10f} |' for p in ('P1', 'P2', 'P3'))
    atomic_text(output/'README.md', f'''# 独立1500方案交付

本目录由 `build_p1_tonight_delivery.py` 独立生成，未修改远端基础成果或旧方案库。基础为 `{base_ref}` 中 `{BASE_DIR}`；只有P1、5核的047、075、085三项替换。其余1497份计划JSON载荷与基础归档逐字节一致，P2/P3全部继承。

## 文件

- `selected_plans.tar.gz`：完整100图×3场景×5核数的1500份方案，目录与基础包相同。
- `累计1500配置成绩.csv`：完整成绩，已全表移除旧哈希和旧提交字段，避免改动项携带陈旧值。
- `三项变更.csv`、`统计与验证.json`：变更、各场景各核数均值及覆盖/结构验证。
- `备选/`：按已独立复评候选的耗时与额外COPY筛出的非支配方案，另有全候选审计和精简复评证据。

## 三项替换

| 图 | 耗时变化（周期） | 耗时减少 | 额外COPY变化（字节） |
|---|---:|---:|---:|
{change_lines}

047采用run_v2的beam→CP组合结果；075采用此前接力实验复评结果；085采用run_v2保持原Task划分的束搜索结果（CP返回该初解，没有额外改善）。替换按耗时优先选择，047和075同时增加COPY，因此不声称官方综合总分或全部指标提升。

## 100图五核累计平均加速比

| 场景 | 原库 | 本交付 |
|---|---:|---:|
{group_lines}

计算为每图 `original_singlecore / makespan` 的算术平均，100图均纳入。P1相对均值增幅为 {stats['p1_n5_mean_speedup_relative_increase_percent']:.8f}%。这是历史累计精选库的替换统计，不是新算法从头1500配置成绩，也不是官方综合评分公式。

## 验证范围与备选口径

全包1500项唯一覆盖、JSON结构、compute覆盖、Task依赖与核序联合无环性、核数均已检查；新增三项的op键插入顺序与独立官方复评计划严格一致。打包采用复评计划原始字节，不重新排序操作键。输出归档重新打开后与基础逐项比较，确认1497份未变、仅三项替换。

本次打包新增官方调用为0，没有重跑1500份官方评测。结构检查不能替代全部场景的官方模拟。原库继承已有评价证据，新三项已有独立复评通过，详细证据保留于备选目录。

备选池包括原精选、接力实验、run_v1/run_v2已独立复评的结果，也包括接力时独立重放的区域起点；较慢但COPY更少的方案可能保留。共核对{len(audit)}项候选，其中{len(frontier)}项对耗时/COPY非支配；这{len(frontier)}项包含已入主包的3项，另有{stats['nondominated_additional_to_selected']}项供权衡选择。非支配只针对这个已验证候选池和这两个指标，不表示数学全局Pareto最优，也不据此替换默认求解器。被支配候选仍列在审计CSV中。

## 再生成

在现有Python项目环境运行 `python build_p1_tonight_delivery.py --output 新的交付目录` 可独立重建。脚本只读Git基础包及本地既有复评记录，不调用官方评测、不提交Git、不覆盖原库。
''')
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=HERE/'P1多尺度联合优化_20260926')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--base-ref', default=DEFAULT_BASE_REF)
    args = parser.parse_args()
    root = args.root.resolve()
    output = (args.output or root/'交付1500').resolve()
    result = build(root, output, args.base_ref)
    print(json.dumps({key: result[key] for key in ('complete', 'plans', 'unchanged_payloads',
                     'changed_payloads', 'candidate_audit_count', 'nondominated_alternatives',
                     'p1_n5_mean_speedup_before', 'p1_n5_mean_speedup_after', 'elapsed_seconds')},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
