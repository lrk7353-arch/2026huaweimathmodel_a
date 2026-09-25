"""Frozen P2/P3 cold matrix, reusing identical completed cold runs with costs.

Reuse is of whole from-scratch runs, not historical best-plan warm starts.
Every reused row retains all original calls and elapsed-time records.
"""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import random

from common_run import atomic_json, read_json, score
from frontier_solver import run

HERE=Path(__file__).resolve().parent


def work(task):
    case,p,n,out=task
    s=run(case,p,n,out,24,240,60,'frontier')
    return dict(case=case,problem=p,cores=n,summary=str(Path(out)/'summary.json'),
                reused=False,best=score(s['best_record'])[0] if s['best_record'] else None,
                calls=s['logical_calls'],seconds=s['elapsed_seconds'])


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--reuse',type=Path,nargs='*',default=[])
    p.add_argument('--workers',type=int,default=8);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=False)
    names=['event_frontier.py','frontier_solver.py']
    hashes={n:hashlib.sha256((HERE/n).read_bytes()).hexdigest() for n in names}
    results=[];reused=set()
    for root in a.reuse:
        manifest=read_json(root/'manifest.json')
        assert all(manifest['sources'][n]==hashes[n] for n in names),'source changed'
        for row in read_json(root/'results.json'):
            if row['variant']!='frontier' or row['problem'] not in (2,3):continue
            s=read_json(Path(row['summary']));key=(s['case'],s['problem'],s['cores'])
            assert s['complete'] and s['budget']==24 and s['logical_calls']<=24
            settings=read_json(Path(row['summary']).parent/'input.json')
            assert settings['seconds']==240 and settings['timeout']==60
            if key in reused:continue
            reused.add(key);results.append(dict(case=key[0],problem=key[1],cores=key[2],
                summary=row['summary'],reused=True,best=row['best'],calls=s['logical_calls'],seconds=s['elapsed_seconds']))
    tasks=[(f'case_{i:03d}',p,n,str(a.out/'slots'/f'case_{i:03d}'/f'p{p}_n{n}'))
           for i in range(1,101) for p in (2,3) for n in range(1,6)
           if (f'case_{i:03d}',p,n) not in reused]
    random.Random(20260926).shuffle(tasks)
    atomic_json(a.out/'manifest.json',dict(sources=hashes,tasks=tasks,reused_roots=list(map(str,a.reuse)),
        budget=24,seconds=240,timeout=60,workers=a.workers,seed=20260926,
        scope='100 graphs x 2 problems x 5 core counts; cold, all costs retained; no P1 claim'))
    atomic_json(a.out/'results.json',results);errors=[]
    with concurrent.futures.ProcessPoolExecutor(max_workers=a.workers) as pool:
        pending={pool.submit(work,t):t for t in tasks}
        for job in concurrent.futures.as_completed(pending):
            try:results.append(job.result())
            except Exception as exc:
                errors.append(dict(task=pending[job],error=repr(exc)))
                atomic_json(a.out/'errors.json',errors)
            atomic_json(a.out/'results.json',results)
            if len(results)%25==0:print(json.dumps(dict(completed=len(results),errors=len(errors))),flush=True)
    atomic_json(a.out/'completion.json',dict(complete=len(results)==1000 and not errors,
                completed=len(results),errors=errors,reused=len(reused)))
    print(json.dumps(dict(completed=len(results),errors=len(errors),reused=len(reused))),flush=True)


if __name__=='__main__':main()
