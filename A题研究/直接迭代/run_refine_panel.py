"""Bounded incremental panel across core counts; every slot preserves its incumbent."""
import argparse,time
from concurrent.futures import ProcessPoolExecutor,wait,FIRST_COMPLETED
from common_run import *

def one(j,out,deadline,budget,seconds):
    remaining=min(seconds,deadline-time.monotonic())
    if remaining<=0:return None
    case,p,n=j['case'],j['problem'],j['cores'];target=Path(out)/'slots'/case/f'p{p}_n{n}'
    if p==1:
        from run_p1_warm import run
        return run(case,j['old'],target,budget,remaining,cores=n)
    if p==2:
        from trace_warm import run
        return run(case,j['old'],target,budget,remaining,cores=n)
    from run_p3_refine import run
    return run(case,j['old'],j['p2'],target,budget,remaining,cores=n)

def main(a):
    out=Path(a.out).resolve()
    if out==DATA or DATA in out.parents:raise ValueError('Output cannot overwrite official data')
    out.mkdir(parents=True,exist_ok=False);start=time.monotonic();deadline=start+a.window
    best=known();jobs=[]
    for c in a.cases:
        case=f'case_{c:03d}'
        for n in a.cores:
            jobs.append({'case':case,'problem':a.problem,'cores':n,'old':best[case,a.problem,n],
                         'p2':best.get((case,2,n))})
    skipped=[]
    if a.exclude_completed:
        previous={(s['case'],s['problem'],s['num_cores']) for f in Path(a.exclude_completed).glob('slots/case_*/p*_n*/summary.json') for s in [read_json(f)]}
        skipped=[j for j in jobs if (j['case'],j['problem'],j['cores']) in previous]
        jobs=[j for j in jobs if (j['case'],j['problem'],j['cores']) not in previous]
    factor=1 if a.order=='small_first' else -1
    jobs.sort(key=lambda j:(factor*(DATA/(j['case']+'.json')).stat().st_size,j['case'],j['cores']))
    atomic_json(out/'plan.json',{'jobs':jobs,'budget':a.budget,'seconds_per_slot':a.seconds,
                               'window':a.window,'workers':a.workers,'skipped_completed':skipped,'order':a.order,'scope':'extra-budget portfolio'})
    pending=iter(jobs);running={};rows=[]
    print(json.dumps({'problem':a.problem,'expected':len(jobs),'budget_per_slot':a.budget}),flush=True)
    def report(final=False):
        completed={(r['case'],r['num_cores']) for r in rows}
        remaining=[(j['case'],j['cores']) for j in jobs if (j['case'],j['cores']) not in completed]
        write_csv(out/'results.csv',rows)
        atomic_json(out/('summary.json' if final else 'progress.json'),{
            'complete':not remaining,'completed':len(rows),'expected':len(jobs),'remaining':remaining,
            'rows':rows,'wall_seconds':time.monotonic()-start,
            'scope':'returned slots may stop on time; unchanged incumbent remains feasible'})
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        def submit():
            if time.monotonic()>=deadline:return
            j=next(pending,None)
            if j is not None:running[pool.submit(one,j,out,deadline,a.budget,a.seconds)]=j
        for _ in range(a.workers):submit()
        while running:
            fs,_=wait(running,return_when=FIRST_COMPLETED)
            for f in fs:
                j=running.pop(f);s=f.result()
                if s is not None:
                    row={k:s.get(k) for k in ('case','problem','num_cores','before','after','logical_calls','new_calls','elapsed_seconds','stop_reason')}
                    row['reduction_pct']=100*(1-row['after']/row['before']);rows.append(row);report()
                    print(json.dumps(row),flush=True)
                submit()
    report(True);print('DONE',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--problem',type=int,choices=(1,2,3),required=True)
    p.add_argument('--cases',type=lambda x:list(range(1,101)) if x=='all' else [int(n) for n in x.split(',')],default=list(range(1,101)))
    p.add_argument('--cores',type=lambda x:[int(n) for n in x.split(',')],default=[2,3,4]);p.add_argument('--budget',type=int,default=4)
    p.add_argument('--seconds',type=float,default=90);p.add_argument('--window',type=float,default=1200);p.add_argument('--workers',type=int,default=2)
    p.add_argument('--exclude-completed');p.add_argument('--order',choices=['small_first','large_first'],default='small_first')
    a=p.parse_args()
    if any(c not in range(1,101) for c in a.cases) or any(n not in range(1,6) for n in a.cores) or min(a.budget,a.seconds,a.window,a.workers)<=0:p.error('Invalid inputs')
    main(a)
