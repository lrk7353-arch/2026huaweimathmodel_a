"""Run a frozen explicit P1 task shard; CPU host information stays in records."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import platform
import random

from common_run import atomic_json, read_json, score
from frontier_solver import run


def work(task):
    case,cores,out=task
    s=run(case,1,cores,out,24,240,60,'mature')
    return dict(case=case,problem=1,cores=cores,variant='mature',summary=str(Path(out)/'summary.json'),
                best=score(s['best_record'])[0] if s['best_record'] else None,
                calls=s['logical_calls'],elapsed_seconds=s['elapsed_seconds'])


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--tasks',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--workers',type=int,default=12)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    keys=read_json(a.tasks)
    assert len(keys)==len({(c,n) for c,n in keys}) and all(1<=n<=5 for c,n in keys)
    tasks=[(c,n,str(a.out/'slots'/c/f'p1_n{n}')) for c,n in keys]
    random.Random(20260926).shuffle(tasks)
    atomic_json(a.out/'manifest.json',dict(tasks=tasks,workers=a.workers,budget=24,seconds=240,timeout=60,
        python=platform.python_version(),platform=platform.platform(),cpu_count=os.cpu_count(),
        scope='P1 mature integrated cold; identical source/data bundle; no historical input plans'))
    results=[];errors=[]
    with concurrent.futures.ProcessPoolExecutor(max_workers=a.workers) as pool:
        jobs={pool.submit(work,t):t for t in tasks}
        for job in concurrent.futures.as_completed(jobs):
            try:results.append(job.result())
            except Exception as exc:errors.append(dict(task=jobs[job],error=repr(exc)))
            atomic_json(a.out/'results.json',results);atomic_json(a.out/'errors.json',errors)
            if len(results)%20==0:print(json.dumps(dict(completed=len(results),total=len(tasks),errors=len(errors))),flush=True)
    atomic_json(a.out/'completion.json',dict(complete=len(results)==len(tasks) and not errors and all(r['best'] is not None for r in results),
        completed=len(results),total=len(tasks),errors=errors,missing_best=sum(r['best'] is None for r in results)))
    print(json.dumps(dict(completed=len(results),total=len(tasks),errors=len(errors))),flush=True)
