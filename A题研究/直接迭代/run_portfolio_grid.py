"""Small from-scratch P1 grid; each core-count shares one explicit call/time cap."""
import argparse,time
from concurrent.futures import ProcessPoolExecutor,as_completed
from common_run import *
from p1_adaptive import run


def one(case,cores,out,budget,seconds):
    s=run(case,cores,'portfolio_diverse',out,budget,seconds,seed=17,evaluation_timeout=60)
    return dict(case=case,problem=1,num_cores=cores,after=score(s['best_record'])[0] if s['best_record'] else None,
                logical_calls=s['logical_calls'],new_calls=s['new_calls'],elapsed_seconds=s['elapsed_seconds'],
                stop_reason=s['stop_reason'])


def main(a):
    out=Path(a.out).resolve();out.mkdir(parents=True,exist_ok=False);start=time.monotonic()
    jobs=[(f'case_{c:03d}',n) for c in a.cases for n in a.cores];rows=[]
    atomic_json(out/'plan.json',dict(jobs=jobs,budget=a.budget,seconds=a.seconds,method='portfolio_diverse',seed=17,scope='targeted expansion after five-core results; not unbiased validation'))
    with ProcessPoolExecutor(max_workers=2) as pool:
        fs=[pool.submit(one,c,n,out/'slots'/c/f'p1_n{n}',a.budget,a.seconds) for c,n in jobs]
        for f in as_completed(fs):
            row=f.result();rows.append(row);write_csv(out/'results.csv',rows)
            atomic_json(out/'progress.json',dict(completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-start));print(json.dumps(row),flush=True)
    atomic_json(out/'summary.json',dict(complete=True,completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-start));print('DONE',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True)
    p.add_argument('--cases',type=lambda s:[int(x) for x in s.split(',')],default=[2,63])
    p.add_argument('--cores',type=lambda s:[int(x) for x in s.split(',')],default=[2,3,4])
    p.add_argument('--budget',type=int,default=12);p.add_argument('--seconds',type=float,default=90)
    main(p.parse_args())
