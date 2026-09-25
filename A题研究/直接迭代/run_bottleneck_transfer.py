"""Frozen from-scratch transfer panel; original vs reserved old/new, budget 12."""
import argparse
import hashlib
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from common_run import *
from p1_portfolio import run as original_run
from p1_portfolio_reserved import run as reserved_run

ROOT=R.parent


def one(job, protocol, root):
    case,n=job['case'],job['cores']; rows=[]; prefixes={}
    for method in ('original','old','new'):
        out=Path(root)/case/f'n{n}'/method
        if method=='original':
            result=original_run(case,n,out,12,180,seed=17,evaluation_timeout=60,
                                structural_diversity=True,evaluation_dir=out/'evaluations')
        else:
            result=reserved_run(case,n,out,12,180,seed=17,refresh=True,bottleneck=method=='new')
            prefixes[method]=[(x['name'],x['record']['status'],x['record']['hashes']['plan_sha256'],
                               x['record'].get('metrics',{}).get('makespan'))
                              for x in result['evaluations'][:result['prefix_calls']]]
        assert result['logical_calls']<=12
        rec=result['best_record']
        row=dict(case=case,cores=n,method=method,makespan=score(rec)[0] if rec else None,
                 added_copy=score(rec)[1] if rec else None,calls=result['logical_calls'],fresh_calls=result['new_calls'],
                 elapsed_seconds=result['elapsed_seconds'],
                 errors=sum(x['record']['status']!='success' for x in result['evaluations']),
                 summary=str(out/'summary.json'))
        rows.append(row);write_csv(out.parent/'results.csv',rows)
        print(json.dumps(row,ensure_ascii=False),flush=True)
    equal=prefixes['old']==prefixes['new']
    atomic_json(Path(root)/case/f'n{n}'/'prefix_check.json',dict(identical=equal,prefixes=prefixes))
    if not equal: raise AssertionError('old/new shared prefix differs')
    return rows


def main():
    p=argparse.ArgumentParser();p.add_argument('--protocol',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();protocol=read_json(a.protocol)
    for rel,digest in protocol['sha256'].items():
        if hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()!=digest: p.error('frozen source changed: '+rel)
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);atomic_json(out/'protocol.json',protocol)
    atomic_json(out/'execution.json',dict(git=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),command=sys.argv))
    rows=[]
    with ProcessPoolExecutor(max_workers=2) as pool:
        fs=[pool.submit(one,job,protocol,str(out)) for job in protocol['jobs']]
        for future in as_completed(fs):
            rows.extend(future.result());write_csv(out/'results.csv',rows)
    atomic_json(out/'summary.json',dict(completed=True,rows=rows,calls=sum(x['calls'] for x in rows)))


if __name__=='__main__':main()
