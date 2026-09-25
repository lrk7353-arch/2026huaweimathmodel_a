#!/usr/bin/env python3
"""Profile graphed matrix/vector workloads separately to verify actual overlap."""
import argparse
import json
from pathlib import Path
import subprocess


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--out',type=Path,required=True)
    args=p.parse_args();args.out.mkdir(parents=True,exist_ok=False)
    records=[]
    for size in [512,1024]:
        for mode in ['serial','parallel']:
            path=args.out/f'm{size}_{mode}';path.mkdir()
            argv=['msprof',f'--output={path.resolve()}/prof','--task-time=on','--runtime-api=on',
                  '--aic-metrics=PipeUtilization','python3',str(Path(__file__).with_name('mixed_resources.py').resolve()),
                  '--graph','--sizes',str(size),'--elements','1048576','--modes',mode,
                  '--samples','1','--warmup','1','--out',str((path/'results').resolve())]
            print('MIXED_PROFILE',size,mode,flush=True)
            with (path/'collector.log').open('w') as f:
                proc=subprocess.run(argv,stdout=f,stderr=subprocess.STDOUT,timeout=240)
            records.append({'size':size,'mode':mode,'argv':argv,'returncode':proc.returncode})
            (args.out/'collection.json').write_text(json.dumps(records,indent=2)+'\n')
            if proc.returncode: print('PROFILE_FAILED inspect log',flush=True)


if __name__=='__main__':
    main()
