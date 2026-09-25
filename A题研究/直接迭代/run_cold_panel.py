"""Full from-scratch P1 search with an initially empty evaluator cache per slot."""
import argparse,time
from concurrent.futures import ProcessPoolExecutor,as_completed
from common_run import *
from p1_adaptive import run


def one(case,out,method):
    out=Path(out)/case/method/'seed17'
    assert not out.exists()
    s=run(case,5,method,out,12,180,seed=17,evaluation_timeout=60,evaluation_dir=out/'evaluations')
    r=s['best_record']
    return dict(case=case,method=method,makespan=score(r)[0] if r else None,
                logical_calls=s['logical_calls'],new_calls=s['new_calls'],elapsed_seconds=s['elapsed_seconds'],
                timeouts=sum(t['record']['status']=='timeout' for t in s['evaluations']),stop_reason=s['stop_reason'])


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--cases',type=lambda s:[f'case_{int(c):03d}' for c in s.split(',')],required=True)
    p.add_argument('--method',default='portfolio_diverse');a=p.parse_args();out=Path(a.out);out.mkdir(parents=True,exist_ok=False);rows=[];start=time.monotonic()
    atomic_json(out/'plan.json',dict(cases=a.cases,method=a.method,budget=12,seconds=180,per_call_seconds=60,
                scope='new evaluator directory per case; full search generation and calls timed; two workers, machine-specific observed wall time'))
    with ProcessPoolExecutor(max_workers=2) as pool:
        fs=[pool.submit(one,c,out,a.method) for c in a.cases]
        for f in as_completed(fs):
            row=f.result();rows.append(row);write_csv(out/'results.csv',rows);atomic_json(out/'progress.json',dict(completed=len(rows),expected=len(a.cases),rows=rows));print(json.dumps(row),flush=True)
    atomic_json(out/'summary.json',dict(complete=True,completed=len(rows),expected=len(a.cases),rows=rows,wall_seconds=time.monotonic()-start))
