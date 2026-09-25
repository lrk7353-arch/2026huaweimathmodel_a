"""Fixed logical-budget P1 comparison; shared caches prohibit cold-time claims."""
import argparse,random,time
from concurrent.futures import ProcessPoolExecutor,as_completed
from collections import defaultdict
from common_run import *
from p1_adaptive import run

def one(job,out):
    case,method,seed=job
    directory=Path(out)/case/method/f'seed{seed}'
    s=run(case,5,method,directory,budget=8,time_budget=86400,seed=seed)
    s['best_record']=s['best']['record'] if s['best'] else None
    atomic_json(directory/'summary.json',s)
    return dict(case=case,method=method,seed=seed,makespan=score(s['best_record'])[0] if s['best_record'] else None,
                logical_calls=s['logical_calls'],new_calls=s['new_calls'],elapsed_seconds=s['elapsed_seconds'],
                timeouts=sum(x['record']['status']=='timeout' for x in s['evaluations']),
                winner_plan=s['best_record']['plan_path'] if s['best_record'] else None)

def main(a):
    out=Path(a.out).resolve();out.mkdir(exist_ok=False,parents=True);start=time.monotonic()
    buckets=defaultdict(list);features=[]
    for c in range(1,101):
        case=f'case_{c:03d}';ir=GraphIR.from_path(DATA/(case+'.json'));count=len(ir.compute_ids)
        if count>6000:continue
        frac=max(max(x.work_m,x.work_v,x.work_other) for x in ir.components)/max(ir.total_work_m,ir.total_work_v,1)
        group=('small' if count<=2000 else 'medium')+('_heavy' if frac>=.5 else '_distributed')
        buckets[group].append(case);features.append(dict(case=case,compute_ops=count,largest_component_fraction=frac,stratum=group))
    rng=random.Random(20260925);cases=[]
    for name,items in sorted(buckets.items()):
        shuffled=sorted(items);rng.shuffle(shuffled);cases+=shuffled[:4]
    cases=sorted(cases);jobs=[(c,m,s) for c in cases for s in (17,42,73) for m in ('component','legacy','adaptive')]
    # Interleave deterministic shuffled order so one method is not always last.
    rng.shuffle(jobs)
    atomic_json(out/'plan.json',dict(cases=cases,eligible_features=features,bucket_sizes={k:len(v) for k,v in buckets.items()},
        jobs=jobs,seeds=[17,42,73],cores=5,logical_cap=8,per_call_timeout=60,
        scope='no historical incumbent; up to8 official calls per method/seed; no active case-wall cutoff; shared success cache; not a cold runtime benchmark',
        selection='at most4 cases per structural stratum; compute ops<=6000; no score-dependent panel selection',
        seed_caveat='some method stages deterministic; report exact plan diversity, not independent-sample confidence intervals'))
    print(json.dumps({'cases':cases,'slots':len(jobs)}),flush=True);rows=[]
    with ProcessPoolExecutor(max_workers=2) as pool:
        fs=[pool.submit(one,j,out) for j in jobs]
        for f in as_completed(fs):
            r=f.result();rows.append(r);write_csv(out/'results.csv',rows)
            atomic_json(out/'progress.json',dict(completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-start))
            print(json.dumps(r),flush=True)
    atomic_json(out/'summary.json',dict(complete=True,completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-start))
    print('DONE',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);main(p.parse_args())
