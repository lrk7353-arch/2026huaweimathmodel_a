"""Audit a completed paired P1 cold panel without running any evaluator."""
import argparse
import gzip
import hashlib
import json
import statistics
from pathlib import Path

from common_run import DATA, GraphIR, atomic_json, read_json, score, validate_plan, write_csv

METHODS = ('integrated', 'joint_tonight')


def ordered_plan(plan):
    return json.dumps(plan, ensure_ascii=False, separators=(',', ':'))


def trace_matches(ir, plan, raw, record):
    cores = len(plan['core_schedules'])
    if raw['num_cores'] != cores or record['metrics']['num_cores'] != cores:
        raise ValueError('official core count differs from plan')
    timelines = raw['per_core_timeline']
    by_core = {entry['core_id']: entry for entry in timelines}
    if len(timelines) != cores or set(by_core) != set(range(cores)):
        raise ValueError('official trace core coverage mismatch')
    mapping = {int(op): task for op, task in plan['node_to_subgraph'].items()}
    found = set()
    for core, tasks in enumerate(plan['core_schedules']):
        if [t['task_id'] for t in by_core[core]['tasks']] != tasks:
            raise ValueError('official trace Task order mismatch')
        for event in by_core[core]['ops']:
            op = event['op_id']
            if op in mapping:
                if op in found or event['task_id'] != mapping[op] or mapping[op] not in tasks:
                    raise ValueError('official trace compute ownership mismatch')
                found.add(op)
    if found != set(ir.compute_ids):
        raise ValueError('official trace compute coverage mismatch')
    for field in ('makespan', 'data_movement_bytes', 'memory_peak_by_core'):
        if raw[field] != record['metrics'][field]:
            raise ValueError('record/raw metric mismatch: ' + field)


def audit_record(ir, embedded, arm_dir):
    path = Path(embedded['record_path']).resolve()
    if not path.is_relative_to(arm_dir.resolve()):
        raise ValueError('paid result was imported from outside this arm')
    record = read_json(path)
    if record['status'] != embedded['status'] or record.get('metrics') != embedded.get('metrics'):
        raise ValueError('embedded record differs from persisted official record')
    plan_path = Path(record['plan_path'])
    plan = read_json(plan_path)
    validate_plan(ir, plan)
    if hashlib.sha256(plan_path.read_bytes()).hexdigest() != record['hashes']['plan_sha256']:
        raise ValueError('persisted candidate plan differs from recorded evaluation input')
    request = read_json(path.parent/'request.json')
    if Path(request['plan_path']).resolve() != plan_path.resolve() or request['problem'] != 1:
        raise ValueError('official request points to another plan/scene')
    if Path(record['graph_path']).resolve() != ir.path.resolve():
        raise ValueError('paid record belongs to another original graph')
    if record['status'] == 'success':
        with gzip.open(record['result_path'], 'rt') as stream:
            raw = json.load(stream)
        trace_matches(ir, plan, raw, record)
    return record, plan


def best_features(record):
    if record is None:
        return dict(makespan=None, added_copy=None, simulated_l1=None, simulated_ub=None, simulated_memory_by_core=None)
    metrics = record['metrics']; memory = metrics['memory_peak_by_core']
    return dict(makespan=metrics['makespan'], added_copy=metrics['data_movement_bytes']['added_copy_bytes'],
                simulated_l1=max(v['L1'] for v in memory.values()), simulated_ub=max(v['UB'] for v in memory.values()),
                simulated_memory_by_core=memory)


def audit_arm(path, config, method, budget):
    summary = read_json(path); calls = summary['calls']; arm_dir = path.parent
    if summary['case'] != config['case'] or summary['problem'] != 1 or summary['num_cores'] != config['cores'] or summary['method'] != method:
        raise ValueError('arm identity differs from preregistered configuration')
    if len(calls) != summary['logical_calls'] or len(calls) > budget or summary['budget'] != budget:
        raise ValueError('charged calls violate arm budget')
    ir = GraphIR.from_path(DATA/(config['case']+'.json'))
    best, histories, prefix, paths, successes, failures, cached = None, [], [], set(), 0, 0, 0
    for index, call in enumerate(calls, 1):
        embedded = call['record']; record, plan = embedded, None
        if embedded.get('record_path'):
            record, plan = audit_record(ir, embedded, arm_dir)
            if record['record_path'] in paths:
                raise ValueError('same official attempt charged twice')
            paths.add(record['record_path'])
        elif embedded['status'] == 'success':
            raise ValueError('successful paid call has no persisted record')
        if call.get('plan_path') and plan is not None and ordered_plan(read_json(call['plan_path'])) != ordered_plan(plan):
            raise ValueError('proposed candidate differs from official evaluated plan')
        cached += bool(record.get('cache_hit', False))
        if record['status'] == 'success':
            successes += 1
            if best is None or score(record) < score(best): best = record
        else: failures += 1
        if call.get('stage') == 'prefix': prefix.append(dict(name=call['name'], plan=ordered_plan(plan) if plan else None))
        histories.append(dict(config=config['id'], case=config['case'], cores=config['cores'], method=method,
                              call=index, stage=call.get('stage'), family=call.get('phase'), name=call['name'],
                              status=record['status'], elapsed_seconds=call.get('elapsed_seconds'), **best_features(best)))
    actual_best = summary.get('best_record')
    if (best is None) != (actual_best is None) or (best and (actual_best['record_path'] not in paths or score(actual_best) != score(best))):
        raise ValueError('reported best is not a best result paid for in this arm')
    if best:
        paid = next(c['record'] for c in calls if c['record'].get('record_path') == actual_best['record_path'])
        if paid['metrics'] != actual_best['metrics']: raise ValueError('best metrics differ from its charged record')
        if ordered_plan(read_json(arm_dir/'best.plan.json')) != ordered_plan(read_json(actual_best['plan_path'])):
            raise ValueError('exported best plan differs from charged winner')
    if summary['new_calls'] != len(calls)-cached or cached:
        raise ValueError('cold panel expected fresh attempts and correct fresh-call ledger')
    generation_errors = [stage for stage in summary['stages'] if stage.get('phase') == 'generation_error']
    row = dict(config=config['id'], case=config['case'], cores=config['cores'], method=method,
               status=summary['status'], actual_calls=len(calls), successes=successes, failures=failures,
               generation_errors=len(generation_errors), generation_error_details=generation_errors,
               elapsed_seconds=summary['elapsed_seconds'], stop_reason=summary['stop_reason'],
               prefix_calls=len(prefix), best_record_path=actual_best['record_path'] if actual_best else None,
               **best_features(actual_best))
    return row, histories, prefix


def reduction(new, old):
    return 100*(1-new/old) if new is not None and old is not None and old > 0 else None


def analyze(root, targeted=False):
    panel_path, summary_path = root/'panel.json', root/'summary.json'
    if not panel_path.exists() or not summary_path.exists():
        print('完整回归结果尚未就绪；本次只读分析退出，不等待或轮询。'); return None
    panel, completed = read_json(panel_path), read_json(summary_path)
    if not completed.get('complete'):
        print('回归尚未全部完成；本次只读分析退出，不汇总部分结果为最终结论。'); return None
    configs = panel['configurations']
    if panel['budget'] != 24 or panel['seconds'] != 240 or (not targeted and len(configs) != 6):
        raise ValueError('expected B24/240 and six configurations unless --targeted is explicit')
    if completed['completed'] != 2*len(configs) or completed['expected'] != 2*len(configs):
        raise ValueError('expected both completed method arms for every configuration')
    rows, curves, pairs, prefix_rows = [], [], [], []
    for config in configs:
        arms, history, prefixes = {}, {}, {}
        for method in METHODS:
            path = root/'slots'/config['id']/method/'summary.json'
            arm, histories, prefix = audit_arm(path, config, method, panel['budget'])
            arms[method], history[method], prefixes[method] = arm, histories, prefix
            rows.append(arm); curves.extend(histories)
        old, new = (arms[method] for method in METHODS)
        common = min(old['actual_calls'], new['actual_calls'])
        co, cn = (history[method][common-1] if common else best_features(None) for method in METHODS)
        prefix_old, prefix_new = (prefixes[method] for method in METHODS)
        actual_prefix_equal = len(prefix_old) == len(prefix_new) and bool(prefix_old)
        six_prefix_calls = len(prefix_old) == len(prefix_new) == 6
        for i in range(max(len(prefix_old), len(prefix_new))):
            a = prefix_old[i] if i < len(prefix_old) else None; b = prefix_new[i] if i < len(prefix_new) else None
            match = bool(a and b and a['plan'] is not None and a['plan'] == b['plan'])
            actual_prefix_equal = actual_prefix_equal and match
            prefix_rows.append(dict(config=config['id'], call=i+1, integrated_name=a['name'] if a else None,
                                    joint_name=b['name'] if b else None, ordered_plan_equal=match))
        pair = dict(config=config['id'], case=config['case'], cores=config['cores'],
                    integrated_time=old['makespan'], joint_time=new['makespan'], reduction_pct=reduction(new['makespan'],old['makespan']),
                    integrated_copy=old['added_copy'], joint_copy=new['added_copy'],
                    integrated_calls=old['actual_calls'], joint_calls=new['actual_calls'],
                    integrated_seconds=old['elapsed_seconds'], joint_seconds=new['elapsed_seconds'],
                    integrated_errors=old['generation_errors'], joint_errors=new['generation_errors'],
                    common_calls=common, integrated_common_time=co['makespan'], joint_common_time=cn['makespan'],
                    common_reduction_pct=reduction(cn['makespan'], co['makespan']),
                    integrated_prefix_calls=len(prefix_old), joint_prefix_calls=len(prefix_new),
                    actual_initial_ordered_plans_equal=actual_prefix_equal,
                    both_initial_six_complete=six_prefix_calls,
                    initial_six_ordered_plans_equal=actual_prefix_equal and six_prefix_calls)
        pairs.append(pair)
    high = [p for p in pairs if p['cores'] == 5]; low = [p for p in pairs if p['cores'] != 5]
    if not targeted and (len(high) != 4 or len({p['case'] for p in high}) != 4 or len(low) != 2):
        raise ValueError('panel does not contain four n5 graphs and two lower-core configurations')
    gains = [p['reduction_pct'] for p in high if p['reduction_pct'] is not None]
    matched = [p['common_reduction_pct'] for p in high if p['common_reduction_pct'] is not None]
    aggregate = dict(targeted_post_selection=targeted, n5_graphs=len(high), successful_n5_pairs=len(gains), n5_mean_paired_reduction_pct=statistics.mean(gains) if gains else None,
                     n5_common_call_mean_paired_reduction_pct=statistics.mean(matched) if matched else None,
                     n5_wins=sum(x>0 for x in gains), n5_ties=sum(x==0 for x in gains), n5_losses=sum(x<0 for x in gains),
                     lower_core_pairs=low, actual_calls=sum(r['actual_calls'] for r in rows),
                     all_actual_initial_ordered_plans_equal=all(p['actual_initial_ordered_plans_equal'] for p in pairs),
                     all_initial_six_complete=all(p['both_initial_six_complete'] for p in pairs),
                     all_initial_six_ordered_plans_equal=all(p['initial_six_ordered_plans_equal'] for p in pairs))
    if aggregate['actual_calls'] != completed['actual_official_calls']:
        raise ValueError('panel call total differs from audited arm ledgers')
    output = root/'离线诊断'; output.mkdir(exist_ok=True)
    write_csv(output/'各臂指标.csv',rows); write_csv(output/'成对结果.csv',pairs)
    write_csv(output/'逐调用最好曲线.csv',curves); write_csv(output/'初始化逐项比较.csv',prefix_rows)
    result = dict(aggregate=aggregate, pairs=pairs, arms=rows,
        scope=('Targeted follow-up selected after observed results; separate from the original panel and not independent validation. ' if targeted else 'Four distinct graphs, six configurations, twelve cold arms. ') + 'Same upper budgets do not imply equal actual calls or wall time. Cold winners are not selected-library improvements; no official aggregate score is inferred.')
    atomic_json(output/'诊断.json', result)
    fmt = lambda value: '无可行结果' if value is None else f'{value:.4f}'
    introduction = ('本次是观察原回归结果后选定的定向修复复跑，单独报告，不并入原六配置均值，也不视为独立验证。' if targeted else '这是4张不同图的6个配置、12个实验臂；结果为统一预算算法比较，不等于累计精选方案提升，也不构造官方总分。')
    lines = ['# '+('定向修复冷启动复跑' if targeted else '六配置完整冷启动回归'), '', introduction, '',
             '|配置|integrated周期|joint_tonight周期|配对降时%|实际调用旧/新|共同调用处降时%|', '|---|---:|---:|---:|---:|---:|']
    for p in pairs:
        lines.append(f"|{p['config']}|{p['integrated_time']}|{p['joint_time']}|{fmt(p['reduction_pct'])}|{p['integrated_calls']}/{p['joint_calls']}|{fmt(p['common_reduction_pct'])}|")
    lines += ['', f"本次{len(high)}张五核图：{aggregate['n5_wins']}胜、{aggregate['n5_ties']}平、{aggregate['n5_losses']}负；平均配对降时 {fmt(aggregate['n5_mean_paired_reduction_pct'])}%。有效配对{len(gains)}/{len(high)}。",
              f"共同实际调用数处，五核平均配对降时 {fmt(aggregate['n5_common_call_mean_paired_reduction_pct'])}%。低核配置（若有）见表，未当作额外独立图并入该均值。", '',
              f"总付费调用{aggregate['actual_calls']}，生成错误{sum(r['generation_errors'] for r in rows)}，失败调用{sum(r['failures'] for r in rows)}。",
              '两法实际初始化完整有序plan逐项一致：'+('是。' if aggregate['all_actual_initial_ordered_plans_equal'] else '否；详见初始化逐项比较.csv。'),
              '所有配置都用满6个初始化调用：'+('是。' if aggregate['all_initial_six_complete'] else '否；不足6个的实际前缀长度见成对结果.csv，同样长度且逐项相同不算初始化差异。'), '',
              '已验证每个成功结果的官方record、请求plan和原始trace相符，最终best来自本臂付费调用且为本臂最优；每臂最多24次调用。实际耗时、COPY与模拟L1/UB峰值见各臂指标.csv。共同调用数比较仍不代表相同CPU时间。']
    (output/'回归结论.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps(aggregate, ensure_ascii=False, indent=2)); return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=Path(__file__).resolve().parent/'P1多尺度联合优化_20260926/cold_panel')
    parser.add_argument('--targeted', action='store_true', help='Explicitly label a post-selected follow-up panel separately')
    args = parser.parse_args(); analyze(args.run.resolve(), targeted=args.targeted)
