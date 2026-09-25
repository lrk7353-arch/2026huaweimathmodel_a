"""Close the two attack rounds against their single frozen strong baseline."""
import argparse
import gzip
import json
from pathlib import Path
import shutil
import statistics

from common_run import atomic_json,read_json,write_csv
from audit_unified_results import validate_record


def summarize(panel,runs,out):
    out.mkdir(parents=True,exist_ok=True)
    initial={(x['case'],x['problem']):x for x in read_json(panel)['items']}
    assert len(initial)==30 and len(runs)==2
    rows,attempts,rounds=[],[],[]
    selected={}
    for index,run in enumerate(runs,1):
        complete=read_json(run/'completion.json')
        assert complete['completed']==30 and not complete['errors']
        rounds.append(complete)
        shutil.copyfile(run/'completion.json',out/f'第{index}轮汇总.json')
        shutil.copyfile(run/'manifest.json',out/f'第{index}轮运行配置.json')
        for path in sorted(run.rglob('summary.json')):
            s=read_json(path);k=s['case'],s['problem'];baseline=initial[k]
            rec=s['best_record'];validate_record(rec,raw=True)
            selected[k]=rec
            rows.append(dict(round=index,case=k[0],problem=k[1],cores=5,
                frozen_before=baseline['makespan'],round_before=s['before'],after=s['after'],
                cumulative_reduction=1-s['after']/baseline['makespan'],round_reduction=s['reduction'],
                selected_name=s['best_name'],new_calls=s['new_calls'],failures=s['failures'],
                selected_plan_sha256=rec['hashes']['plan_sha256'],source_summary=str(path)))
            for a in s['evaluations']:
                r=a['record']
                attempts.append(dict(round=index,case=k[0],problem=k[1],name=a['name'],
                    metadata=a.get('metadata',{}),accepted=a['accepted'],status=r['status'],
                    metrics=r['metrics'],hashes=r['hashes'],cache_hit=r['cache_hit'],
                    seconds=r['elapsed_seconds'],error=r.get('error'),record_path=r['record_path']))
    write_csv(out/'两轮逐配置结果.csv',rows)
    write_csv(out/'两轮逐调用账本.csv',[{k:v for k,v in a.items() if k not in ('metadata','metrics','hashes')}|
        dict(makespan=a['metrics'].get('makespan'),plan_sha256=a['hashes'].get('plan_sha256')) for a in attempts])
    with (out/'两轮候选证据.json.gz').open('wb') as f,gzip.GzipFile(fileobj=f,mode='wb',mtime=0) as z:
        z.write(json.dumps(attempts,ensure_ascii=False,separators=(',',':')).encode())
    last=[r for r in rows if r['round']==2]
    mean=statistics.mean(r['cumulative_reduction'] for r in last)
    substantial=sorted({r['case'] for r in last if r['cumulative_reduction']>=.05})
    result=dict(cells_per_round=30,rounds=rounds,cumulative_mean_reduction=mean,
        graphs_over_five_percent=substantial,threshold_mean=.03,threshold_graphs=3,
        obvious_improvement=mean>=.03 and len(substantial)>=3,
        two_round_official_calls=sum(not a['cache_hit'] for a in attempts),
        two_round_failures=sum(a['status']!='success' for a in attempts),
        groups=[dict(problem=p,mean_reduction=statistics.mean(r['cumulative_reduction'] for r in last if r['problem']==p),
            wins=sum(r['problem']==p and r['cumulative_reduction']>0 for r in last)) for p in (1,2,3)],
        goal_action='continue only if substantial' if mean>=.03 and len(substantial)>=3 else 'terminate as requested after archiving',
        scope='Warm development panel; never a full-pool cold algorithm score. Retaining old best prevents returned regressions; negative candidates are explicitly recorded.')
    atomic_json(out/'两轮最终判定.json',result)
    print(json.dumps(result,ensure_ascii=False))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--panel',type=Path,required=True);p.add_argument('--runs',type=Path,nargs=2,required=True)
    p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    summarize(a.panel,a.runs,a.out)
