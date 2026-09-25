"""One changed P1 policy against completed, fully charged cold baseline arms."""
import argparse
import concurrent.futures
import hashlib
from pathlib import Path
import random

from common_run import atomic_json,read_json,score
from p1_stagnation_portfolio import run


def work(task):
    case,cores,out=task;out=Path(out);out.mkdir(parents=True,exist_ok=False)
    s=run(case,1,cores,'integrated',out/'solver',24,240,60)
    s.update(cores=cores,variant='frontier',complete=True,
        generation_seconds=sum(x.get('generation_seconds',0) for x in s['stages']),generation_errors=[])
    atomic_json(out/'summary.json',s)
    return dict(case=case,problem=1,cores=cores,variant='frontier',summary=str(out/'summary.json'),
                best=score(s['best_record'])[0] if s['best_record'] else None,
                calls=s['logical_calls'],elapsed_seconds=s['elapsed_seconds'])


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--workers',type=int,default=2);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=False)
    baseline=[dict(x,reused=True) for x in read_json(a.baseline/'results.json') if x['variant']=='mature']
    assert len(baseline)==16 and all(x['problem']==1 and x['cores']==5 for x in baseline)
    tasks=[(x['case'],x['cores'],str(a.out/'slots'/x['case']/f'p1_n{x["cores"]}')) for x in baseline]
    random.Random(20260926).shuffle(tasks)
    names=['p1_stagnation_portfolio.py','p1_late_policy.py','barrier_bands.py','critical_contract.py','run_p1_gated_panel.py']
    atomic_json(a.out/'manifest.json',dict(tasks=tasks,baseline=str(a.baseline),budget=24,seconds=240,timeout=60,
        workers=a.workers,sources={n:hashlib.sha256(Path(__file__).with_name(n).read_bytes()).hexdigest() for n in names},
        scope='developmental paired comparison; original paid mature arms reused; lookback6 and last4 gate frozen'))
    results=list(baseline);atomic_json(a.out/'results.json',results)
    with concurrent.futures.ProcessPoolExecutor(max_workers=a.workers) as pool:
        for row in pool.map(work,tasks):
            results.append(row);atomic_json(a.out/'results.json',results);print(row,flush=True)
