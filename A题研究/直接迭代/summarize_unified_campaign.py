"""Compare paired unified runs and cumulative archives without mixing budgets."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
from common_run import atomic_json, read_json, write_csv

HERE = Path(__file__).resolve().parent
LEDGER = HERE / '闭环验证_20260925/下一阶段攻坚/重划实证_v1/累计1500配置成绩.csv'


def summarize(root, out, baseline_run=None):
    manifest = read_json(root / 'manifest.json')
    expected = {(c, p, n, v) for c in manifest['cases'] for p in manifest['problems']
                for n in manifest['cores'] for v in manifest['variants']}
    paths = list(root.glob('slots/*/*/*/summary.json'))
    if baseline_run:
        reference = read_json(baseline_run / 'manifest.json')
        for field in ('cases', 'problems', 'cores', 'budget', 'seconds', 'timeout', 'seed'):
            if reference[field] != manifest[field]:
                raise ValueError('baseline settings mismatch: '+field)
        expected.update((c, p, n, 'baseline') for c in manifest['cases'] for p in manifest['problems'] for n in manifest['cores'])
        paths += list(baseline_run.glob('slots/*/*/baseline/summary.json'))
    slots = {}
    attempts = []
    for path in sorted(paths):
        s = read_json(path)
        key = s['case'], s['problem'], s['num_cores'], s['variant']
        if key in slots or key not in expected:
            raise ValueError('unexpected or duplicate slot')
        if s['graph_sha256'] != manifest['inputs'][s['case']]:
            raise ValueError('input hash mismatch')
        slots[key] = s
        for idx, c in enumerate(s['evaluations']):
            rec = c['record']
            attempts.append(dict(case=s['case'], problem=s['problem'], cores=s['num_cores'], variant=s['variant'],
                call=idx+1, name=c['name'], new_strategy=c['metadata'].get('new_strategy', False),
                status=rec['status'], makespan=rec['metrics'].get('makespan'),
                added_copy=rec['metrics'].get('data_movement_bytes', {}).get('added_copy_bytes'),
                evaluation_seconds=rec['elapsed_seconds'], peak_rss=rec.get('peak_memory_bytes'),
                generation_seconds=c['generation_seconds'], accepted=c['accepted'], record_path=rec['record_path']))
    archive = {(r['case'], int(r['problem']), int(r['cores'])): r
               for r in csv.DictReader(LEDGER.open(encoding='utf-8-sig'))}
    pairs = []
    for case, p, n in sorted({k[:3] for k in slots}):
        b, v = slots.get((case, p, n, 'baseline')), slots.get((case, p, n, 'v2'))
        if not b or not v:
            continue
        old, new = b['best_record'], v['best_record']
        t0, t1 = (old['metrics']['makespan'] if old else None), (new['metrics']['makespan'] if new else None)
        history = int(archive[case, p, n]['makespan']) if (case, p, n) in archive else None
        result = 'invalid' if t1 is None else 'baseline_invalid' if t0 is None else 'win' if t1 < t0 else 'loss' if t1 > t0 else 'tie'
        pairs.append(dict(case=case, problem=p, cores=n, baseline=t0, v2=t1, result=result,
            reduction=1-t1/t0 if t0 and t1 else None, archive=history,
            archive_reduction=1-t1/history if history and t1 else None,
            baseline_calls=b['logical_calls'], v2_calls=v['logical_calls'],
            baseline_seconds=b['elapsed_seconds'], v2_seconds=v['elapsed_seconds'],
            v2_best=v['best']['name'] if v['best'] else None,
            v2_new_calls=sum(c['metadata'].get('new_strategy', False) for c in v['evaluations']),
            v2_generation_errors=json.dumps(v['generation_errors'], ensure_ascii=False)))
    by_problem = {}
    for p in manifest['problems']:
        rows = [r for r in pairs if r['problem'] == p]
        valid = [r for r in rows if r['reduction'] is not None]
        by_problem[str(p)] = dict(pairs=len(rows), wins=sum(r['result']=='win' for r in rows),
            ties=sum(r['result']=='tie' for r in rows), losses=sum(r['result']=='loss' for r in rows),
            invalid=sum(r['result']=='invalid' for r in rows),
            mean_time_reduction_valid_pairs=sum(r['reduction'] for r in valid)/len(valid) if valid else None,
            archive_wins=sum(r['archive_reduction'] is not None and r['archive_reduction']>0 for r in rows))
    summary = dict(expected=len(expected), completed=len(slots), missing=sorted(expected-set(slots)),
        problems=by_problem, official_calls=sum(s['new_calls'] for s in slots.values()),
        source_manifest_sha256=hashlib.sha256((root/'manifest.json').read_bytes()).hexdigest(),
        baseline_reference=str(baseline_run) if baseline_run else None,
        new_run_official_calls=sum(read_json(p)['new_calls'] for p in root.glob('slots/*/*/*/summary.json')),
        failed_calls=sum(r['status']!='success' for r in attempts),
        scope='Paired cold search uses identical maximum calls/time. Historical archive comparison has different accumulated solve cost.')
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out/'配对结果.csv', pairs)
    write_csv(out/'全部候选.csv', attempts)
    atomic_json(out/'结论.json', summary)
    lines = ['# 统一调度器批次对照', '', summary['scope'], '',
             f"完成 {len(slots)}/{len(expected)} 个对照槽位，本次运行新增官方调用 {summary['new_run_official_calls']} 次；包含引用基线的记录合计 {summary['official_calls']} 次。", '',
             '|问题|胜|平|负|无有效解|有效配对平均降时|超过累计库|', '|---|---:|---:|---:|---:|---:|---:|']
    for p, d in by_problem.items():
        reduction = '—' if d['mean_time_reduction_valid_pairs'] is None else f"{100*d['mean_time_reduction_valid_pairs']:.3f}%"
        lines.append(f"|P{p}|{d['wins']}|{d['ties']}|{d['losses']}|{d['invalid']}|{reduction}|{d['archive_wins']}|")
    lines += ['', '失败、超时和负例均列入全部候选。无有效解不计入降时均值，必须单独处理，不能据此宣称整体晋级。',
              '五核开发面板不代表全部100图、2—5核泛化结果；最终统一版本与累计精选成果单独交付。']
    (out/'批次效果.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--baseline-run', type=Path)
    a = p.parse_args()
    summarize(a.run, a.out, a.baseline_run)
