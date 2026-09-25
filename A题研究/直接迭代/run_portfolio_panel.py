"""Bounded fifth-round comparisons; small regression and new large graphs separate."""
import argparse,random,time
from concurrent.futures import ProcessPoolExecutor,as_completed
from common_run import *
from p1_adaptive import run


def one(job,out,seconds):
    case,method,seed=job
    s=run(case,5,method,Path(out)/case/method/f'seed{seed}',budget=12,time_budget=seconds,seed=seed,evaluation_timeout=60)
    r=s['best_record']
    return dict(case=case,method=method,seed=seed,makespan=score(r)[0] if r else None,
                logical_calls=s['logical_calls'],new_calls=s['new_calls'],elapsed_seconds=s['elapsed_seconds'],
                timeouts=sum(t['record']['status']=='timeout' for t in s['evaluations']),
                stop_reason=s['stop_reason'],winner_plan=r['plan_path'] if r else None)


def main(a):
    out=Path(a.out).resolve();out.mkdir(parents=True,exist_ok=False);start=time.monotonic()
    runs=R/'直接迭代/运行结果'
    if a.group=='regression':
        cases=sorted(read_json(runs/'融合开发_v1/plan.json')['cases']+read_json(runs/'融合扩展_v1/plan.json')['cases'])
        seeds=[17,42,73];seconds=86400
    elif a.group=='large':
        cases=read_json(runs/'综合计划_v1/large_selection.json')['cases'];seeds=[17];seconds=180
    elif a.group=='extension':
        cases=read_json(runs/'缓存计划_v1/large_selection.json')['cases'];seeds=[17];seconds=180
    else:cases=['case_016'];seeds=[17];seconds=180
    methods=a.methods;jobs=[(c,m,s) for c in cases for m in methods for s in seeds]
    random.Random(20260927).shuffle(jobs)
    atomic_json(out/'plan.json',dict(group=a.group,cases=cases,methods=methods,reference=methods[-1],seeds=seeds,jobs=jobs,
        cores=5,logical_cap=12,per_call_timeout=60,case_soft_seconds=seconds,
        scope='no historical incumbent; shared success cache; equal logical cap; large group also has180s soft limit so report time censoring',
        routing='component/selective prefix at most8; up to2 promising tensor probes; skip Task refinements for single Task; merge8/16 first for many tiny Tasks; total cap12'))
    print(json.dumps({'group':a.group,'cases':cases,'slots':len(jobs)}),flush=True);rows=[]
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(one,j,out,seconds) for j in jobs]
        for f in as_completed(futures):
            r=f.result();rows.append(r);write_csv(out/'results.csv',rows)
            atomic_json(out/'progress.json',dict(completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-start))
            print(json.dumps({k:v for k,v in r.items() if k!='winner_plan'}),flush=True)
    atomic_json(out/'summary.json',dict(complete=True,completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-start))
    print('DONE',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--group',choices=['regression','large','extension','diagnostic'],required=True);p.add_argument('--out',required=True)
    p.add_argument('--methods',type=lambda s:s.split(','),default=['adaptive','hybrid','portfolio'])
    main(p.parse_args())
