#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path
import random
import statistics as st


def main():
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);args=p.parse_args()
    rows=list(csv.DictReader((args.directory/'raw.csv').open()))
    assert all(r['correct']=='True' for r in rows)
    results=[]
    for size,elements in sorted({(int(r['size']),int(r['elements'])) for r in rows}):
        groups={mode:{int(r['sample']):float(r['device_envelope_us']) for r in rows
                      if int(r['size'])==size and int(r['elements'])==elements and r['mode']==mode}
                for mode in ['matrix_only','vector_only','serial','parallel']}
        common=sorted(set(groups['serial'])&set(groups['parallel']))
        ratios=[groups['serial'][i]/groups['parallel'][i] for i in common]
        rng=random.Random(20260926)
        boot=sorted(st.median(rng.choices(ratios,k=len(ratios))) for _ in range(1000))
        row={'size':size,'elements':elements,'samples':len(common)}
        row.update({mode+'_median_us':st.median(g.values()) for mode,g in groups.items()})
        row.update(ratio_of_medians=row['serial_median_us']/row['parallel_median_us'],
                   paired_ratio_median=st.median(ratios),ci_low=boot[24],ci_high=boot[974])
        results.append(row)
    with (args.directory/'summary.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=results[0]);w.writeheader();w.writerows(results)
    print(json.dumps(results,indent=2))


if __name__=='__main__': main()
