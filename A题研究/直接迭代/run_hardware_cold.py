"""Freeze and execute a paired cold comparison, with shared budgets per arm."""
import argparse
import concurrent.futures
import hashlib
from pathlib import Path
import random

from common_run import atomic_json, score
from hardware_solver import run


def work(task):
    case,p,n,variant,out,budget,seconds,timeout=task
    s=run(case,p,n,out,budget,seconds,timeout,variant)
    return dict(case=case,problem=p,cores=n,variant=variant,calls=s['logical_calls'],
        best=score(s['best_record'])[0] if s['best_record'] else None,
        elapsed_seconds=s['elapsed_seconds'],summary=str(Path(out)/'summary.json'))


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--cases',default='9,13,23,28,35,37,44,46,49,53,55,61,66,71,92,95')
    p.add_argument('--problems',default='2,3');p.add_argument('--cores',type=int,default=5)
    p.add_argument('--budget',type=int,default=24);p.add_argument('--seconds',type=float,default=240)
    p.add_argument('--timeout',type=float,default=60);p.add_argument('--workers',type=int,default=4)
    p.add_argument('--variants',default='mature,frontier');args=p.parse_args()
    args.out.mkdir(parents=True,exist_ok=False)
    cases=[f'case_{int(x):03d}' for x in args.cases.split(',')]
    tasks=[(case,problem,args.cores,variant,str(args.out/'slots'/case/f'p{problem}_n{args.cores}'/variant),
            args.budget,args.seconds,args.timeout)
           for case in cases for problem in map(int,args.problems.split(',')) for variant in args.variants.split(',')]
    random.Random(20260926).shuffle(tasks)
    atomic_json(args.out/'manifest.json',dict(tasks=tasks,budget=args.budget,seconds=args.seconds,workers=args.workers,
        scope='frozen cold comparison; structural coverage, not an absolute unseen-graph test',
        sources={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                 for name in ['event_frontier.py','frontier_solver.py','hardware_solver.py','barrier_bands.py','run_hardware_cold.py']}))
    results=[]
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        jobs=[pool.submit(work,task) for task in tasks]
        for job in concurrent.futures.as_completed(jobs):
            result=job.result();results.append(result);atomic_json(args.out/'results.json',results)
            print(result,flush=True)


if __name__=='__main__':main()
