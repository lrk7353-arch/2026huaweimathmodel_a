#!/usr/bin/env python3
"""Fresh-process confirmation of exploratory best and worst, plus doubled input."""
import argparse
import csv
import json
from pathlib import Path
import subprocess
import time


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--input',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--binary',type=Path,default=Path(__file__).parent/'build/mechanism_bench')
    p.add_argument('--samples',type=int,default=30)
    p.add_argument('--suites',default='pipe,barrier,reuse,cache')
    args=p.parse_args()
    args.out.mkdir(parents=True,exist_ok=False)
    overview=json.loads((args.input/'overview.json').read_text())
    provenance=[]
    suites=args.suites.split(',')
    for suite in suites:
        original={r['id']:r for r in csv.DictReader((args.input/f'{suite}_config.csv').open())}
        chosen=[]
        for kind in ['best','worst']:
            pair=overview[suite][kind]
            for scale in [1,2]:
                for key in ['baseline','candidate']:
                    source=original[pair[key]]
                    row=dict(source)
                    row['n']=int(row['n'])*scale
                    row['id']=f"{suite}_{kind}_scale{scale}_{source['id']}"
                    chosen.append(row)
                    provenance.append({'id':row['id'],'source':source['id'],'selection':kind,'n_scale':scale})
        cfg=args.out/f'{suite}_config.csv'
        with cfg.open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=chosen[0].keys());w.writeheader();w.writerows(chosen)
    (args.out/'manifest.json').write_text(json.dumps({
        'source_batch':str(args.input),'selection':'best and worst exploratory ratio, original and doubled n',
        'scope':'confirmation, not held-out graph validation','samples':args.samples,'warmup':10,
        'selection_provenance':provenance},indent=2)+'\n')
    for suite in suites:
        print('CONFIRM',suite,flush=True)
        with (args.out/f'{suite}.log').open('w') as log:
            subprocess.run([str(args.binary.resolve()),str((args.out/f'{suite}_config.csv').resolve()),
                            str((args.out/f'{suite}_raw.csv').resolve()),str(args.samples),'10'],
                           stdout=log,stderr=subprocess.STDOUT,check=True,timeout=1800)
    print('CONFIRM_COMPLETE',flush=True)


if __name__=='__main__':
    main()
