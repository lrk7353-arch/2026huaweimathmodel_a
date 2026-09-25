"""Run a fixed original-graph panel, with fresh independent evaluation stores."""
import argparse
import random
from concurrent.futures import ProcessPoolExecutor,as_completed
from common_run import *
from cold_portfolio import run


def one(job,root,budget,seconds,timeout):
    case,p,n,m=job['case'],job['problem'],job['cores'],job['method']
    out=root/'slots'/case/f'p{p}_n{n}'/m
    try:
        if m=='baseline':
            if p==1:
                from p1_adaptive import run as old
                s=old(case,n,'portfolio_diverse',out,budget,seconds,evaluation_timeout=timeout,evaluation_dir=out/'evaluations')
            else:
                from p23_pipeline import run as old
                s=old(case,p,n,'trace_routed',out,budget,seconds,evaluation_timeout=timeout,evaluation_dir=out/'evaluations')
        else:s=run(case,p,n,m,out,budget,seconds,timeout,out/'evaluations')
        calls=s.get('calls',s.get('evaluations',[]));best=s['best_record']
        return dict(**job,status='success' if best else 'no_feasible_result',makespan=score(best)[0] if best else None,
            logical_calls=s['logical_calls'],new_calls=s['new_calls'],elapsed_seconds=s['elapsed_seconds'],stop_reason=s['stop_reason'],
            failed_calls=sum(c['record']['status']!='success' for c in calls),
            generation_errors=sum('error' in stage for stage in s.get('stages',[])),summary_path=str((out/'summary.json').resolve()))
    except Exception as exc:
        import traceback
        atomic_json(out/'error.json',dict(error=repr(exc),traceback=traceback.format_exc()))
        return dict(**job,status='exception',error=repr(exc))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--inputs',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--workers',type=int,default=2)
    a=p.parse_args();plan=read_json(a.inputs);a.out=a.out.resolve()
    if a.out==DATA or DATA in a.out.parents:raise ValueError('output inside official data')
    a.out.mkdir(parents=True,exist_ok=False);jobs=list(plan['jobs']);random.Random(17).shuffle(jobs)
    atomic_json(a.out/'plan.json',dict(**plan,execution_order=jobs,fresh_evaluations=True));started=time.monotonic();rows=[]
    def save(done=False):
        atomic_json(a.out/('summary.json' if done else 'progress.json'),dict(complete=done,completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-started))
        write_csv(a.out/'results.csv',rows)
    save()
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        fs=[pool.submit(one,j,a.out,plan['budget'],plan['seconds'],plan['evaluation_timeout']) for j in jobs]
        for f in as_completed(fs):
            r=f.result();rows.append(r);save();print(json.dumps(r,ensure_ascii=False),flush=True)
    save(True)
    if any(r['status']=='exception' for r in rows):raise RuntimeError('panel contains exceptions')


if __name__=='__main__':main()
