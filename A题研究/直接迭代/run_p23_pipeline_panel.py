"""Bounded, fresh-evaluation comparison of P2/P3 graph-to-plan pipelines."""
import argparse
import random
from concurrent.futures import ProcessPoolExecutor, as_completed
from common_run import *
from p23_pipeline import run


def one(case, problem, method, out, budget, seconds, reuse_evaluations=False, cores=5):
    started = time.monotonic()
    out = Path(out)
    try:
        s = run(case, problem, cores, method, out, budget, seconds,
                evaluation_dir=None if reuse_evaluations else out/'evaluations', evaluation_timeout=60)
        record = s['best_record']
        return dict(case=case, problem=problem, cores=cores, method=method, status=s['status'],
                    effective_method=s['effective_method'],
                    makespan=score(record)[0] if record else None,
                    logical_calls=s['logical_calls'], new_calls=s['new_calls'],
                    elapsed_seconds=s['elapsed_seconds'], stop_reason=s['stop_reason'],
                    failed_calls=sum(c['record']['status']!='success' for c in s['calls']),
                    timeout_calls=sum(c['record']['status']=='timeout' for c in s['calls']),
                    generation_errors=sum('error' in stage for stage in s['stages']),
                    summary_path=str(out/'summary.json'), error=None)
    except Exception as exc:
        import traceback
        error=dict(case=case, problem=problem, cores=cores, method=method, status='error',effective_method=None,
                   makespan=None, logical_calls=None, new_calls=None,
                   elapsed_seconds=time.monotonic()-started, stop_reason='exception',
                   failed_calls=None, timeout_calls=None, generation_errors=None,
                   summary_path=str(out/'summary.json'), error=repr(exc))
        atomic_json(out/'error.json', dict(**error, traceback=traceback.format_exc()))
        return error


def main(a):
    out=Path(a.out).resolve()
    if out==DATA or DATA in out.parents or out.exists():raise ValueError('Use a fresh directory outside official data')
    if a.budget<1 or a.seconds<=0 or a.workers<1:raise ValueError('Positive budgets/workers required')
    out.mkdir(parents=True)
    cases=[f'case_{c:03d}' for c in a.cases]
    jobs=[(c,p,m) for c in cases for p in a.problems for m in a.methods]
    random.Random(17).shuffle(jobs)
    atomic_json(out/'plan.json',dict(cases=cases,problems=a.problems,methods=a.methods,cores=a.cores,
        seed=17,budget=a.budget,seconds=a.seconds,per_call_seconds=60,workers=a.workers,jobs=jobs,
        fresh_evaluations=not a.reuse_evaluations,scope='No incumbent; first official evaluation included; cache hits charged; shared cache only with --reuse-evaluations; fixed seed17.'))
    started=time.monotonic();rows=[]
    def save(final=False):
        s=dict(complete=final,completed=len(rows),expected=len(jobs),
            failed=sum(r['status']!='success' for r in rows),rows=rows,wall_seconds=time.monotonic()-started)
        atomic_json(out/('summary.json' if final else 'progress.json'),s)
        write_csv(out/'results.csv',rows)
    save()
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        futures=[pool.submit(one,c,p,m,out/c/f'p{p}_n{a.cores}'/m,a.budget,a.seconds,a.reuse_evaluations,a.cores) for c,p,m in jobs]
        for future in as_completed(futures):
            row=future.result();rows.append(row);save();print(json.dumps(row,ensure_ascii=False),flush=True)
    save(True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases',required=True,type=lambda s:[int(c) for c in s.split(',')])
    p.add_argument('--out',required=True);p.add_argument('--budget',type=int,default=12)
    p.add_argument('--seconds',type=float,default=120);p.add_argument('--workers',type=int,default=2)
    p.add_argument('--methods',type=lambda s:s.split(','),default=['component_wcc','staged'])
    p.add_argument('--problems',type=lambda s:[int(x) for x in s.split(',')],default=[2,3])
    p.add_argument('--cores',type=int,choices=range(1,6),default=5)
    p.add_argument('--reuse-evaluations',action='store_true')
    a=p.parse_args()
    if not a.cases or len(set(a.cases))!=len(a.cases) or any(c not in range(1,101) for c in a.cases):p.error('Use unique cases 1..100')
    if not a.methods or len(set(a.methods))!=len(a.methods) or any(m not in ('component_wcc','staged','routed','trace_routed') for m in a.methods):p.error('Use unique valid methods')
    if not a.problems or len(set(a.problems))!=len(a.problems) or any(n not in (2,3) for n in a.problems):p.error('Use unique problems 2 and/or 3')
    main(a)
