"""Compare bounded P3 refiners from exactly the same recorded incumbents."""
import argparse,time
from concurrent.futures import ProcessPoolExecutor,as_completed
from common_run import *


def one(case,old,out,method,budget,seconds,cores):
    if method=='joint':
        from p3_joint import run
        return run(case,old,out,budget,seconds,cores)
    if method=='read_order':
        from p3_read_order import run
        return run(case,old,out,budget,seconds,cores)
    from run_p3_refine import run
    return run(case,old,None,out,budget,seconds,cores)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--before',required=True);p.add_argument('--out',required=True)
    p.add_argument('--cases',type=lambda s:[f'case_{int(c):03d}' for c in s.split(',')],required=True)
    p.add_argument('--method',choices=['read_order','legacy','joint'],default='read_order');p.add_argument('--cores',type=int,default=5)
    p.add_argument('--budget',type=int,default=12);p.add_argument('--seconds',type=float,default=120);p.add_argument('--workers',type=int,default=2)
    a=p.parse_args();out=Path(a.out);out.mkdir(parents=True,exist_ok=False);start=time.monotonic()
    before={key(r):r for r in read_json(a.before)['records']};rows=[]
    atomic_json(out/'plan.json',vars(a))
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        fs=[pool.submit(one,c,before[c,3,a.cores],out/c,a.method,a.budget,a.seconds,a.cores) for c in a.cases]
        for future in as_completed(fs):
            s=future.result();row={k:s[k] for k in ('case','before','after','logical_calls','new_calls','elapsed_seconds','stop_reason')};rows.append(row)
            write_csv(out/'results.csv',rows);atomic_json(out/'progress.json',dict(completed=len(rows),expected=len(a.cases),rows=rows));print(json.dumps(row),flush=True)
    atomic_json(out/'summary.json',dict(complete=True,completed=len(rows),expected=len(a.cases),rows=rows,wall_seconds=time.monotonic()-start))
