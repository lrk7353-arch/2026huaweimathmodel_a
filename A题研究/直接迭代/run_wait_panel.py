"""Execute the frozen 18-configuration, three-arm warm comparison."""
import csv
import hashlib
import random
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor,as_completed
from pathlib import Path
from common_run import R,read_json,atomic_json,write_csv
from wait_search import run

BASE=Path(__file__).parent;OUT=BASE/'三指标联合推进_20260926/集中对照'
CASES={1:[44,47,64,69,51,75],2:[53,62,80,18,9,90],3:[17,23,80,44,46,90]}


def worker(job):
    p,i,policy=job;case=f'case_{i:03d}';out=OUT/'slots'/f'{case}_p{p}'/policy
    path=BASE/'联合整合_20260926/最终累计/方案'/f'p{p}'/'n5'/f'{case}_multicore_res.json'
    s=read_json(out/'summary.json') if (out/'summary.json').exists() else run(case,p,5,read_json(path),out,policy)
    r=s['best_record'];m=r['metrics'];d=m['data_movement_bytes'];c=m.get('cache_stats',{})
    return dict(case=case,problem=p,cores=5,policy=policy,makespan=m['makespan'],added_copy=d['added_copy_bytes'],
        spill=d['spill_added_copy_bytes'],hit_rate=c.get('hit_rate'),hit_bytes=c.get('hit_bytes'),miss_bytes=c.get('miss_bytes'),
        calls=s['logical_calls'],new_calls=s['new_calls'],failures=s['failures'],generation_failures=s['generation_failures'],
        generation_seconds=s['generation_seconds'],elapsed_seconds=s['elapsed_seconds'],summary=str(out/'summary.json'))


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    sources={str(p.relative_to(R.parent)):hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in (BASE,R/'solver',R/'advanced_solver',R/'精修求解器') for p in folder.glob('*.py')}
    manifest=dict(cases=CASES,policies=['mature','proxy','wait_joint'],budget=12,seconds=180,
        timeout=45,generation_timeout=20,workers=4,seed=17,sources=sources,
        git_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip())
    if (OUT/'manifest.json').exists():assert read_json(OUT/'manifest.json')['sources']==sources,'source changed; do not mix runs'
    else:atomic_json(OUT/'manifest.json',manifest)
    jobs=[(p,i,policy) for p,ids in CASES.items() for i in ids for policy in manifest['policies']]
    random.Random(17).shuffle(jobs);rows=[];errors=[];started=time.monotonic()
    with ProcessPoolExecutor(max_workers=4) as pool:
        futures={pool.submit(worker,j):j for j in jobs}
        for future in as_completed(futures):
            try:
                row=future.result();rows.append(row);print(row,flush=True)
            except Exception as exc:errors.append(dict(job=futures[future],error=repr(exc)));print(errors[-1],flush=True)
            write_csv(OUT/'逐臂结果.csv',rows)
            atomic_json(OUT/'progress.json',dict(completed=len(rows),errors=errors,elapsed_seconds=time.monotonic()-started))
    atomic_json(OUT/'execution.json',dict(complete=len(rows)==54,completed=len(rows),errors=errors,
        elapsed_seconds=time.monotonic()-started,calls=sum(r['calls'] for r in rows)))
    assert not errors,errors


if __name__=='__main__':main()
