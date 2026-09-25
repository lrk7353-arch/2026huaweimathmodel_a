"""Frozen from-scratch comparison: upstream, reserved single, reserved iterative."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import subprocess
from common_run import *
from p1_portfolio import run as original_run
from p1_portfolio_reserved import run as reserved_run
from p1_adaptive import plan_key

ROOT=R.parent


def one(job,protocol,root):
    case,n=job['case'],job['cores'];rows=[]
    for method in job['methods']:
        out=Path(root)/case/f'n{n}'/method
        if method=='original':
            s=original_run(case,n,out,protocol['budget'],protocol['seconds'],
                seed=protocol['seed'],evaluation_timeout=60,structural_diversity=True,
                evaluation_dir=out/'evaluations')
        else:
            s=reserved_run(case,n,out,protocol['budget'],protocol['seconds'],
                           seed=protocol['seed'],refresh=method=='iterative')
        assert s['logical_calls']<=protocol['budget']
        rec=s['best_record']
        prefix=[]
        if method!='original':
            for x in s['evaluations'][:s['prefix_calls']]:
                prefix.append(dict(name=x['name'],status=x['record']['status'],
                    plan_sha256=hashlib.sha256(plan_key(read_json(x['record']['plan_path'])).encode()).hexdigest(),
                    time=score(x['record'])[0] if x['record']['status']=='success' else None))
            atomic_json(out/'prefix_certificate.json',prefix)
        row=dict(case=case,cores=n,method=method,after=score(rec)[0] if rec else None,
            added_copy=score(rec)[1] if rec else None,status='success' if rec else 'no_feasible_result',
            logical_calls=s['logical_calls'],new_calls=s['new_calls'],
            errors=sum(x['record']['status'] not in ('success','timeout') for x in s['evaluations']),
            timeouts=sum(x['record']['status']=='timeout' for x in s['evaluations']),
            elapsed_seconds=s['elapsed_seconds'],
            task_calls=sum(x['phase']=='task_refine' for x in s['evaluations']),
            prefix_calls=s['prefix_calls'],summary=str(out/'summary.json'))
        rows.append(row);write_csv(out.parent/'results.csv',rows)
        print(json.dumps(row,ensure_ascii=False),flush=True)
    parent=Path(root)/case/f'n{n}'
    equal=read_json(parent/'single/prefix_certificate.json')==read_json(parent/'iterative/prefix_certificate.json')
    atomic_json(parent/'paired_prefix_check.json',dict(identical=equal))
    return rows


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--protocol',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();protocol=read_json(a.protocol)
    for rel,digest in {**protocol['python_sha256'],**protocol['official_sha256']}.items():
        if hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()!=digest:
            p.error('frozen file changed: '+rel)
    out=a.out.resolve()
    if out==DATA or DATA in out.parents:p.error('output outside official data required')
    out.mkdir(parents=True,exist_ok=False)
    atomic_json(out/'protocol.json',protocol)
    atomic_json(out/'execution.json',dict(command=sys.argv,
        protocol_sha256=hashlib.sha256(a.protocol.read_bytes()).hexdigest(),
        git_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()))
    started=time.monotonic();rows=[];failures=[]
    with ProcessPoolExecutor(max_workers=protocol['workers']) as pool:
        fs={pool.submit(one,j,protocol,str(out)):j for j in protocol['jobs']}
        for future in as_completed(fs):
            try:rows.extend(future.result())
            except Exception as exc:
                failures.append(dict(job=fs[future],error=repr(exc)))
                for pending in fs:pending.cancel()
                break
            write_csv(out/'results.csv',rows)
            atomic_json(out/'progress.json',dict(completed=len(rows),expected=3*len(protocol['jobs']),elapsed_seconds=time.monotonic()-started))
    result=dict(completed=len(rows)==3*len(protocol['jobs']) and not failures,rows=rows,failures=failures,
        elapsed_seconds=time.monotonic()-started,logical_calls=sum(x['logical_calls'] for x in rows),
        actual_new_calls=sum(not read_json(p)['cache_hit'] for p in out.rglob('record.json')))
    atomic_json(out/'summary.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
