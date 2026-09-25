"""Give one bounded extra stage to winning arms still improving near their cap.

This is adaptive portfolio construction, not an equal-budget algorithm test.
The input panels must be complete; selection and source records are saved.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from common_run import *
from refine_regions import run


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--panels',nargs='+',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--budget',type=int,default=12);p.add_argument('--seconds',type=float,default=60)
    a=p.parse_args();winners={}
    for panel in a.panels:
        s=read_json(panel/'summary.json')
        if not s['complete'] or s['completed']!=s['expected']:raise ValueError('incomplete input panel')
        for row in s['rows']:
            if row['status']!='success':raise ValueError('failed input arm')
            r=read_json(row['summary_path']);k=key(r['best_record'])
            if k not in winners or score(r['best_record'])<score(winners[k]['best_record']):winners[k]=r
    jobs=[]
    for k,s in sorted(winners.items()):
        times=[];best=s['before']
        for i,c in enumerate(s['calls'],1):
            if c['record']['status']=='success' and score(c['record'])[0]<best:
                best=score(c['record'])[0];times.append(i)
        if (s['stop_reason']=='call_budget' and times and times[-1]>2*s['budget']/3):
            jobs.append(dict(case=k[0],problem=k[1],cores=k[2],policy=s['policy'],
                             record=s['best_record'],last_time_improvement_call=times[-1]))
    a.out.mkdir(parents=True,exist_ok=False);start=time.monotonic()
    atomic_json(a.out/'plan.json',dict(jobs=jobs,budget=a.budget,seconds=a.seconds,expected=len(jobs),
        selection='best arm per slot, strict time gain in final third; one extra stage only',
        scope='adaptive extra-budget portfolio, not equal-budget validation'))
    rows=[]
    with ProcessPoolExecutor(max_workers=2) as pool:
        fs={}
        for j in jobs:
            out=a.out/'slots'/j['case']/f'p{j["problem"]}_n{j["cores"]}'/j['policy']
            f=pool.submit(run,j['case'],j['record'],out,a.budget,a.seconds,j['cores'],j['policy'],None,25)
            fs[f]=out
        for f in as_completed(fs):
            s=f.result();row={k:s[k] for k in ('case','problem','num_cores','policy','before','after','logical_calls','new_calls','failed_calls','elapsed_seconds','generation_seconds','stop_reason')}
            row.update(status='success',summary_path=str((fs[f]/'summary.json').resolve()));rows.append(row)
            atomic_json(a.out/'progress.json',dict(completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-start))
            print(json.dumps(row,ensure_ascii=False),flush=True)
    write_csv(a.out/'results.csv',rows)
    atomic_json(a.out/'summary.json',dict(complete=True,completed=len(rows),expected=len(jobs),rows=rows,wall_seconds=time.monotonic()-start))


if __name__=='__main__':main()
