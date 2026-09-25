"""Bounded P1 incumbent Task refinement, separate from from-scratch comparisons."""
import argparse,gzip,time
from concurrent.futures import ProcessPoolExecutor,as_completed
from common_run import *
from p1_task_refine import generate

def run(case,old,out,budget=6,seconds=150,cores=5,seed=17,merge_caps=None):
    if key(old)!=(case,1,cores):raise ValueError('incumbent case/problem/core mismatch')
    out=Path(out);out.mkdir(parents=True,exist_ok=False);start=time.monotonic();deadline=start+seconds
    ir=GraphIR.from_path(DATA/(case+'.json'));plan=read_json(old['plan_path']);best=old
    with gzip.open(old['result_path'],'rt') as f:raw=json.load(f)
    def exact(p):return json.dumps(p,ensure_ascii=False,separators=(',',':'))
    seen={exact(plan)};trials=[];skips=[];generated=generate(ir,plan,raw,seed,merge_caps=merge_caps)
    atomic_json(out/'candidates.json',[{'name':c['name'],**c['metadata']} for c in generated])
    for c in generated:
        if len(trials)>=budget or time.monotonic()>=deadline:break
        sig=exact(c['plan'])
        if sig in seen:skips.append({'name':c['name'],'reason':'exact_duplicate'});continue
        seen.add(sig);meta=c['metadata']
        if meta['lower_bound']>score(best)[0]:skips.append({'name':c['name'],'reason':'necessary_bound',**meta});continue
        validate_plan(ir,c['plan'])
        rec=evaluate(DATA/(case+'.json'),c['plan'],1,R/'advanced_solver/runs/formal_v2/evaluations',
                     timeout=min(50,max(.1,deadline-time.monotonic())),config_path=DATA/'config.txt')
        if rec['status']=='success' and meta['lower_bound']>score(rec)[0]:raise AssertionError('unsafe lower bound')
        accept=rec['status']=='success' and score(rec)<score(best)
        trials.append({'name':c['name'],'record':rec,'accepted':accept,'metadata':meta})
        if accept:best=rec;plan=c['plan']
        atomic_json(out/'progress.json',{'case':case,'before':score(old)[0],'after':score(best)[0],'calls':len(trials)})
    atomic_json(out/'best.plan.json',plan)
    result=dict(case=case,problem=1,num_cores=cores,before=score(old)[0],after=score(best)[0],best_record=best,
                evaluations=trials,skipped=skips,logical_calls=len(trials),new_calls=sum(not t['record']['cache_hit'] for t in trials),
                elapsed_seconds=time.monotonic()-start,stop_reason='time_budget' if time.monotonic()>=deadline else 'budget_or_pool',
                scope='extra-budget fixed-incumbent Task neighborhood; measured duration is only a scheduling heuristic')
    atomic_json(out/'summary.json',result);return result

def main(a):
    out=Path(a.out).resolve();out.mkdir(exist_ok=False,parents=True);start=time.monotonic();best=known()
    jobs=[(f'case_{c:03d}',n) for c in a.cases for n in a.cores]
    atomic_json(out/'plan.json',dict(jobs=jobs,budget=a.budget,seconds=a.seconds,seed=a.seed,workers=a.workers,merge_caps=a.merge_caps));rows=[]
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        fs=[pool.submit(run,c,best[c,1,n],out/'slots'/c/f'p1_n{n}',a.budget,a.seconds,n,a.seed,a.merge_caps) for c,n in jobs]
        for f in as_completed(fs):
            s=f.result();row={k:s[k] for k in ('case','problem','num_cores','before','after','logical_calls','new_calls','elapsed_seconds','stop_reason')};rows.append(row)
            write_csv(out/'results.csv',rows);atomic_json(out/'progress.json',dict(completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-start));print(json.dumps(row),flush=True)
    atomic_json(out/'summary.json',dict(complete=True,completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-start));print('DONE',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True)
    p.add_argument('--cases',type=lambda x:[int(v) for v in x.split(',')],default=[14,62,16,72,76,87,91,25,53,3,9,63]);p.add_argument('--cores',type=lambda x:[int(v) for v in x.split(',')],default=[5])
    p.add_argument('--merge-caps',type=lambda x:[int(v) for v in x.split(',')],help='Optional merge-only diagnostic; default keeps the original full neighborhood')
    p.add_argument('--budget',type=int,default=6);p.add_argument('--seconds',type=float,default=150);p.add_argument('--seed',type=int,default=17);p.add_argument('--workers',type=int,default=2);main(p.parse_args())
