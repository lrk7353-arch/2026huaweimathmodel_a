"""Run independent warm-start arms from a fixed, reviewable input table."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from common_run import *
from refine_regions import run


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inputs',required=True,type=Path);p.add_argument('--out',required=True,type=Path)
    p.add_argument('--policies',default='legacy,joint');p.add_argument('--budget',type=int,default=24)
    p.add_argument('--seconds',type=float,default=180);p.add_argument('--workers',type=int,default=2)
    p.add_argument('--evaluation-timeout',type=float,default=45)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False);started=time.monotonic()
    records=read_json(a.inputs)['records'];jobs=[(r,policy) for r in records for policy in a.policies.split(',')]
    atomic_json(a.out/'plan.json',dict(inputs=str(a.inputs.resolve()),policies=a.policies,budget=a.budget,
        seconds=a.seconds,workers=a.workers,evaluation_timeout=a.evaluation_timeout,expected=len(jobs)))
    rows=[]
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        fs={}
        for r,policy in jobs:
            c,problem,n=key(r);out=a.out/'slots'/c/f'p{problem}_n{n}'/policy
            fs[pool.submit(run,c,r,out,a.budget,a.seconds,n,policy,None,a.evaluation_timeout)]=(c,problem,n,policy)
        for f in as_completed(fs):
            c,problem,n,policy=fs[f]
            try:
                s=f.result()
                row={k:s[k] for k in ('case','problem','num_cores','policy','before','after','logical_calls','new_calls','failed_calls','elapsed_seconds','generation_seconds','stop_reason')}
                row['status']='success';row['summary_path']=str((a.out/'slots'/c/f'p{problem}_n{n}'/policy/'summary.json').resolve())
            except Exception as exc:
                row=dict(case=c,problem=problem,num_cores=n,policy=policy,status='exception',error=repr(exc))
            rows.append(row);write_csv(a.out/'results.csv',rows)
            atomic_json(a.out/'progress.json',dict(completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-started))
            print(json.dumps(row,ensure_ascii=False),flush=True)
    result=dict(complete=True,rows=rows,completed=len(rows),expected=len(jobs),wall_seconds=time.monotonic()-started)
    atomic_json(a.out/'summary.json',result)
    if any(r['status']!='success' for r in rows):raise RuntimeError('panel has failed jobs; inspect summary')


if __name__=='__main__':main()
