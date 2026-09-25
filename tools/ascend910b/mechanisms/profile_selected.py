#!/usr/bin/env python3
"""Whole-application profiling, without per-kernel replay changing cache history."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import time


def select(root):
    out=[]
    for suite in ['pipe','barrier','reuse','cache','sharing']:
        if not (root/f'{suite}_config.csv').exists(): continue
        rows=list(csv.DictReader((root/f'{suite}_config.csv').open()))
        for row in rows:
            n,b,t,r,g,k=[int(row[key]) for key in ['n','blocks','tile','rounds','groups','repeats']]
            keep=False
            if suite=='pipe':
                keep=(n==1<<22 and b==1 and t==512 and r==16) or (
                    n==1<<20 and b==1 and t==512 and r==1) or (
                    n==1<<22 and b==32 and t==4096 and r==1)
            elif suite=='barrier':
                keep=n==1<<20 and r==64 and (b,g) in [(1,2),(4,4),(16,4)]
            elif suite=='reuse':
                keep=n==1<<20 and r==1 and (b,k) in [(32,8),(1,32)]
            elif suite=='cache':
                keep=n==1<<22 and b==32 and g in [4,64]
            elif suite=='sharing':
                keep=r==1 and ((n==31<<20 and b==32 and t==4096) or
                               (n==31<<18 and b==1 and t==1024))
            if keep: out.append((suite,row))
    return out


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--input',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--binary',type=Path,default=Path(__file__).parent/'build/mechanism_bench')
    p.add_argument('--limit',type=int,default=0)
    args=p.parse_args()
    args.out.mkdir(parents=True,exist_ok=False)
    rows=select(args.input)
    if args.limit: rows=rows[:args.limit]
    results=[]
    for suite,row in rows:
        path=args.out/row['id'];path.mkdir()
        config=path/'config.csv'
        with config.open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=row.keys());w.writeheader();w.writerow(row)
        # L2 collection preserves original ordering: ordinary application profiling,
        # not msprof op's default warmups and kernel replay.
        metric='L2Cache' if suite in ['cache','sharing'] else 'PipeUtilization'
        argv=['msprof',f'--output={path.resolve()}/prof','--task-time=on','--runtime-api=on',
              f'--aic-metrics={metric}',str(args.binary.resolve()),str(config.resolve()),
              str((path/'timed_under_profiler.csv').resolve()),'1','1']
        before=time.monotonic()
        print('PROFILE',row['id'],metric,flush=True)
        with (path/'collector.log').open('w') as log:
            proc=subprocess.run(argv,stdout=log,stderr=subprocess.STDOUT,timeout=240)
        results.append({'id':row['id'],'suite':suite,'config':row,'argv':argv,'returncode':proc.returncode,
                        'seconds':time.monotonic()-before})
        (args.out/'collection.json').write_text(json.dumps(results,indent=2)+'\n')
        if proc.returncode:
            print('COLLECTOR_FAILURE inspect log; retaining other observations',flush=True)
        else:
            print('PROFILE_DONE',row['id'],flush=True)
    (args.out/'binary_sha256.txt').write_text(hashlib.sha256(args.binary.read_bytes()).hexdigest()+'\n')


if __name__=='__main__':
    main()
