"""Bounded official re-evaluation: lower-core inheritance, P2/P3 reuse, N1 coverage."""
import argparse
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from common_run import *
from core_inheritance import pad_plan


def exact(plan):
    return json.dumps(plan, ensure_ascii=False, separators=(',', ':'))


def jobs_for(mode, best, cores):
    jobs = []
    if mode == 'single':
        for case in (f'case_{i:03d}' for i in range(1, 101)):
            for problem in (1, 2, 3):
                if (case, problem, 1) not in best:
                    jobs.append(dict(case=case, problem=problem, cores=1, old=None, source=None))
    else:
        for (case, problem, n), old in sorted(best.items()):
            if n not in cores:
                continue
            if mode == 'inherit':
                sources = [r for (c, p, k), r in best.items()
                           if c == case and p == problem and k < n and score(r) < score(old)]
                if not sources:
                    continue
                source = min(sources, key=score)
            else:
                if problem not in (2, 3):
                    continue
                source = best.get((case, 5 - problem, n))
                if source is None or exact(read_json(source['plan_path'])) == exact(read_json(old['plan_path'])):
                    continue
            jobs.append(dict(case=case, problem=problem, cores=n, old=old, source=source))
    # Start expensive graphs early; ordering never changes plan contents or scores.
    return sorted(jobs, key=lambda j: (-(DATA/(j['case']+'.json')).stat().st_size,
                                       j['case'], j['problem'], j['cores']))


def one(job, root, deadline, timeout):
    start = time.monotonic()
    case, p, n = job['case'], job['problem'], job['cores']
    out = root/'slots'/case/f'p{p}_n{n}'
    out.mkdir(parents=True, exist_ok=True)
    ir = GraphIR.from_path(DATA/(case+'.json'))
    if job['source'] is None:
        if job.get('single_layout')=='bounded':
            from single_core_tasks import bounded_plan
            plan=bounded_plan(ir)
        elif job.get('single_layout')=='components':
            from solver.plan import component_groups,plan_from_component_groups
            plan=plan_from_component_groups(ir,component_groups(ir,[0]*len(ir.components),1,'per_component'))
        else:
            plan = {'node_to_subgraph': {str(op): 0 for op in ir.compute_ids}, 'core_schedules': [[0]]}
    else:
        plan = pad_plan(read_json(job['source']['plan_path']), n)
    validate_plan(ir, plan)
    if time.monotonic() >= deadline:
        return None
    rec = evaluate(DATA/(case+'.json'), plan, p, R/'advanced_solver/runs/formal_v2/evaluations',
                   timeout=min(timeout, max(.1, deadline-time.monotonic())), config_path=DATA/'config.txt')
    old = job['old']
    accepted = rec['status'] == 'success' and (old is None or score(rec) < score(old))
    best = rec if accepted else old
    s = dict(case=case, problem=p, num_cores=n, status=rec['status'],
             before=score(old)[0] if old else None, after=score(best)[0] if best else None,
             accepted=accepted, new_coverage=accepted and old is None,
             best_record=best, record=rec, logical_calls=1, new_calls=int(not rec['cache_hit']),
             source_cores=key(job['source'])[2] if job['source'] else None,
             source_problem=job['source']['problem'] if job['source'] else None,
             elapsed_seconds=time.monotonic()-start)
    if best:
        atomic_json(out/'best.plan.json', read_json(best['plan_path']))
    atomic_json(out/'summary.json', s)
    return s


def main(a):
    out = Path(a.out).resolve()
    if out == DATA or DATA in out.parents:
        raise ValueError('Output cannot be official input directory.')
    start = time.monotonic(); deadline = start+a.seconds
    if out.exists():
        if not a.resume:
            raise ValueError('Use a fresh output directory or explicit --resume.')
        plan = read_json(out/'plan.json')
        if plan['mode'] != a.mode or plan['cores'] != a.cores:
            raise ValueError('Resume mode/cores do not match original plan.')
        jobs = plan['jobs']
    else:
        jobs = jobs_for(a.mode, known(), a.cores)
        jobs = [j for j in jobs if j['problem'] in a.problems and int(j['case'][-3:]) in a.cases]
        for j in jobs:j['single_layout']=a.single_layout
        out.mkdir(parents=True)
        atomic_json(out/'plan.json', dict(mode=a.mode, cores=a.cores, jobs=jobs,
                    scope='extra-budget official portfolio; single mode is coverage, not optimality'))
    results = {}
    def slot(j): return (j['case'], j['problem'], j['cores'])
    for j in jobs:
        p=out/'slots'/j['case']/f'p{j["problem"]}_n{j["cores"]}'/'summary.json'
        if p.exists():
            s=read_json(p)
            if s['status']=='success':results[slot(j)]=s
    pending=iter(j for j in jobs if slot(j) not in results); running={}; calls=[]
    print(json.dumps({'mode':a.mode,'expected':len(jobs),'already_done':len(results)}),flush=True)
    def report(final=False):
        rr=list(results.values())
        rows=[{k:r[k] for k in ('case','problem','num_cores','status','before','after','accepted',
                               'new_coverage','logical_calls','new_calls','elapsed_seconds')} for r in rr]
        write_csv(out/'results.csv', rows)
        s=dict(complete=len(rr)==len(jobs), all_success=len(rr)==len(jobs) and all(r['status']=='success' for r in rr),
               completed=len(rr), expected=len(jobs), rows=rows, wall_seconds=time.monotonic()-start,
               this_invocation_logical_calls=len(calls), this_invocation_new_calls=sum(r['new_calls'] for r in calls),
               remaining=[list(slot(j)) for j in jobs if slot(j) not in results],
               failures=[list(k) for k,r in results.items() if r['status']!='success'])
        atomic_json(out/('summary.json' if final else 'progress.json'), s)
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        def submit():
            if time.monotonic() >= deadline:return
            j=next(pending,None)
            if j is not None:running[pool.submit(one,j,out,deadline,a.timeout)]=j
        for _ in range(a.workers):submit()
        while running:
            finished,_=wait(running,return_when=FIRST_COMPLETED)
            for f in finished:
                j=running.pop(f);s=f.result()
                if s is not None:
                    results[slot(j)]=s;calls.append(s);report()
                    print(json.dumps({k:s[k] for k in ('case','problem','num_cores','status','before','after','accepted')}),flush=True)
                submit()
    report(True)
    print('DONE',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--mode',choices=['inherit','cross','single'],required=True)
    p.add_argument('--out',required=True);p.add_argument('--cores',type=lambda x:[int(n) for n in x.split(',')],default=[2,3,4])
    p.add_argument('--seconds',type=float,default=1200);p.add_argument('--timeout',type=float,default=90)
    p.add_argument('--workers',type=int,default=2);p.add_argument('--resume',action='store_true')
    p.add_argument('--single-layout',choices=['whole','components','bounded'],default='whole')
    p.add_argument('--problems',type=lambda x:[int(n) for n in x.split(',')],default=[1,2,3])
    p.add_argument('--cases',type=lambda x:list(range(1,101)) if x=='all' else [int(n) for n in x.split(',')],default=list(range(1,101)))
    a=p.parse_args()
    if a.seconds<=0 or a.timeout<=0 or a.workers not in (1,2,3,4) or any(n not in range(1,6) for n in a.cores):p.error('Invalid budgets/cores/workers')
    main(a)
