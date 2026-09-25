"""Report broad-screen evidence without conflating cold and cumulative scores."""
import argparse
import json
from pathlib import Path
import statistics
from common_run import atomic_json, read_json, write_csv
from export_unified_campaign import read_csv


def report(run, archive, out):
    completion = read_json(run/'completion.json')
    assert completion['completed'] == completion['total'] and not completion['errors']
    old = {(r['case'],int(r['problem']),int(r['cores'])):r for r in read_csv(archive)}
    rows = read_csv(run/'results.csv')
    assert len(rows) == completion['total']
    details, groups = [], []
    development = {25,43,47,48,51,64,71,75,85}
    for r in rows:
        k=r['case'],int(r['problem']),int(r['cores'])
        a=old[k]
        valid=r['valid']=='True'
        t=int(r['makespan']) if valid else None
        details.append(dict(case=k[0], problem=k[1], cores=k[2], valid=valid,
            cold_makespan=t, archive_makespan=int(a['makespan']),
            cold_speedup=float(a['original_singlecore'])/t if t else None,
            archive_speedup=float(a['speedup']),
            outcome='invalid' if not valid else 'win' if t<int(a['makespan']) else 'tie' if t==int(a['makespan']) else 'loss',
            trained_graph=int(k[0].split('_')[-1]) in development,
            calls=int(r['calls']), seconds=float(r['seconds']), failures=int(r['failures']),
            best_name=r['best_name']))
    for scope in ('all','development9','other91'):
        for p in (1,2,3):
            part=[r for r in details if r['problem']==p and (scope=='all' or r['trained_graph']==(scope=='development9'))]
            good=[r for r in part if r['valid']]
            groups.append(dict(scope=scope,problem=p,count=len(part),valid=len(good),
                wins=sum(r['outcome']=='win' for r in part), ties=sum(r['outcome']=='tie' for r in part),
                losses=sum(r['outcome']=='loss' for r in part),
                cold_mean=statistics.mean(r['cold_speedup'] for r in good) if good else None,
                archive_mean_same_valid=statistics.mean(r['archive_speedup'] for r in good) if good else None))
    out.mkdir(parents=True,exist_ok=True)
    write_csv(out/'宽筛逐配置.csv',details)
    write_csv(out/'宽筛分组汇总.csv',groups)
    result=dict(completion=completion,groups=groups, interpretation='Cold budget-limited solver versus historical cumulative archive; not an equal-budget comparison. The other91 group was not used in the frozen experience fit, but is not a pristine external test set.')
    atomic_json(out/'宽筛结论.json',result)
    text=['# 100图三题五核宽筛','','从头算法与历史累计库分别核算；下表不是同预算胜负。','',
        '|范围|问题|有效/总数|新算法均值|历史累计均值|胜/平/负|','|---|---|---:|---:|---:|---:|']
    for g in groups:
        text.append(f"|{g['scope']}|P{g['problem']}|{g['valid']}/{g['count']}|{g['cold_mean']:.8f}|{g['archive_mean_same_valid']:.8f}|{g['wins']}/{g['ties']}/{g['losses']}|")
    text+=['','均值均为原始单核时间/对应多核时间；P3正式Cache收益需最终同核P2/P3数据另算。',
        f"官方新调用 {completion['official_calls']}，全部执行段合计 {completion.get('all_segments_elapsed_seconds',completion['elapsed_seconds']):.1f} 秒。",
        'development9为参与冻结经验训练的9图；other91未参与这次经验拟合，但仍属于公开开发图池，不称为严格盲测。']
    (out/'宽筛效果.md').write_text('\n'.join(text)+'\n',encoding='utf-8')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path,required=True);p.add_argument('--archive',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();print(json.dumps(report(a.run,a.archive,a.out),ensure_ascii=False))
