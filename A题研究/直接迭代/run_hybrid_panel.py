"""P1 component/adaptive/hybrid from-scratch comparison with one total call cap."""
import argparse, random, time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from common_run import *
from p1_adaptive import run


def one(job, out):
    case, method, seed = job
    directory = Path(out)/case/method/f'seed{seed}'
    s = run(case,5,method,directory,budget=12,time_budget=86400,seed=seed)
    r = s['best_record']
    return dict(case=case,method=method,seed=seed,makespan=score(r)[0] if r else None,
                logical_calls=s['logical_calls'],new_calls=s['new_calls'],
                task_calls=sum(t['phase']=='task_refine' for t in s['evaluations']),
                elapsed_seconds=s['elapsed_seconds'],timeouts=sum(t['record']['status']=='timeout' for t in s['evaluations']),
                winner_plan=r['plan_path'] if r else None)


def main(a):
    out=Path(a.out).resolve();out.mkdir(parents=True,exist_ok=False);started=time.monotonic()
    old=read_json(R/'直接迭代/运行结果/深化等预算_v1/plan.json')
    if a.group=='development':cases=old['cases']
    else:
        rng=random.Random(20260926);buckets=defaultdict(list)
        for f in old['eligible_features']:
            if f['case'] not in old['cases']:buckets[f['stratum']].append(f['case'])
        cases=[]
        for group,items in sorted(buckets.items()):
            items=sorted(items);rng.shuffle(items);cases+=items[:2]
        cases.sort()
    methods=['component','adaptive','hybrid'];seeds=[17,42,73]
    jobs=[(c,m,s) for c in cases for m in methods for s in seeds]
    random.Random(20260926).shuffle(jobs)
    atomic_json(out/'plan.json',dict(group=a.group,cases=cases,methods=methods,reference='hybrid',
        seeds=seeds,jobs=jobs,cores=5,logical_cap=12,per_call_timeout=60,
        scope='no historical incumbent; shared cache; same logical call cap, not cold walltime comparison',
        selection='development reuses prior16; extension selects up to2 per structural stratum excluding prior16; seed20260926',
        hybrid='reserve4 of12 calls; prefix uses at most8 component+selective calls; unused prefix allowance also available to Task neighborhoods; remaining allowance after local exhaustion returns to structural candidates'))
    print(json.dumps({'group':a.group,'cases':cases,'slots':len(jobs)}),flush=True);rows=[]
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(one,j,out) for j in jobs]
        for future in as_completed(futures):
            row=future.result();rows.append(row)
            write_csv(out/'results.csv',rows)
            atomic_json(out/'progress.json',dict(completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-started))
            print(json.dumps(row),flush=True)
    atomic_json(out/'summary.json',dict(complete=True,completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-started))
    print('DONE',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--group',choices=('development','extension'),required=True)
    main(p.parse_args())
