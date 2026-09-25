"""Bounded warm exploration of graph-derived tensor regions; official scores only."""
import argparse,time
from concurrent.futures import ProcessPoolExecutor,as_completed
from common_run import *
from p1_tensor_regions import generate


def run(case,cores,old,out,budget=3,seconds=150):
    if key(old)!=(case,1,cores):raise ValueError('mismatched incumbent')
    out=Path(out);out.mkdir(parents=True,exist_ok=False);start=time.monotonic();deadline=start+seconds
    ir=GraphIR.from_path(DATA/(case+'.json'));best=old;trials=[];skipped=[]
    cs=sorted(generate(ir,cores),key=lambda c:(c['metadata']['lower_bound'],c['metadata']['proxy'],c['name']))
    atomic_json(out/'candidates.json',[{'name':c['name'],**c['metadata']} for c in cs])
    for c in cs:
        if len(trials)>=budget or time.monotonic()>=deadline:break
        if c['metadata']['lower_bound']>score(best)[0]:
            skipped.append({'name':c['name'],**c['metadata']});continue
        rec=evaluate(DATA/(case+'.json'),c['plan'],1,R/'advanced_solver/runs/formal_v2/evaluations',
                     timeout=min(75,max(.1,deadline-time.monotonic())),config_path=DATA/'config.txt')
        if rec['status']=='success' and c['metadata']['lower_bound']>score(rec)[0]:raise AssertionError('unsafe bound')
        accepted=rec['status']=='success' and score(rec)<score(best)
        trials.append(dict(name=c['name'],metadata=c['metadata'],record=rec,accepted=accepted))
        if accepted:best=rec
        atomic_json(out/'progress.json',dict(case=case,cores=cores,calls=len(trials),before=score(old)[0],after=score(best)[0]))
    atomic_json(out/'best.plan.json',read_json(best['plan_path']))
    s=dict(case=case,problem=1,num_cores=cores,before=score(old)[0],after=score(best)[0],best_record=best,
           evaluations=trials,skipped=skipped,logical_calls=len(trials),new_calls=sum(not t['record']['cache_hit'] for t in trials),
           elapsed_seconds=time.monotonic()-start,stop_reason='time_budget' if time.monotonic()>=deadline else 'budget_or_pool',
           scope='targeted extra-budget exploration, not part of hybrid same-call-cap experiment')
    atomic_json(out/'summary.json',s);return s


def main(a):
    out=Path(a.out).resolve();out.mkdir(parents=True,exist_ok=False);start=time.monotonic();best=known()
    jobs=[(f'case_{c:03d}',n) for c in a.cases for n in a.cores];rows=[]
    atomic_json(out/'plan.json',dict(jobs=jobs,budget=a.budget,seconds=a.seconds,per_call_timeout=75,workers=2))
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(run,c,n,best[c,1,n],out/'slots'/c/f'p1_n{n}',a.budget,a.seconds) for c,n in jobs]
        for f in as_completed(futures):
            s=f.result();row={k:s[k] for k in ('case','num_cores','before','after','logical_calls','new_calls','elapsed_seconds','stop_reason')};rows.append(row)
            write_csv(out/'results.csv',rows)
            atomic_json(out/'progress.json',dict(completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-start));print(json.dumps(row),flush=True)
    atomic_json(out/'summary.json',dict(complete=True,completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-start));print('DONE',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True)
    p.add_argument('--cases',type=lambda s:[int(x) for x in s.split(',')],default=[16,3,62])
    p.add_argument('--cores',type=lambda s:[int(x) for x in s.split(',')],default=[5])
    p.add_argument('--budget',type=int,default=3);p.add_argument('--seconds',type=float,default=150)
    main(p.parse_args())
