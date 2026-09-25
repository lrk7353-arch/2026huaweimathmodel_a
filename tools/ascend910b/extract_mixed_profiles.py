#!/usr/bin/env python3
import argparse
import csv
from decimal import Decimal
import json
from pathlib import Path
import shutil


def main():
    p=argparse.ArgumentParser()
    p.add_argument('input',type=Path);p.add_argument('output',type=Path)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    records=json.loads((args.input/'collection.json').read_text())
    shutil.copy2(args.input/'collection.json',args.output/'collection.json')
    summaries=[]
    for record in records:
        key=f"m{record['size']}_{record['mode']}"
        files=list((args.input/key).glob('prof/PROF_*/mindstudio_profiler_output/op_summary_*.csv'))
        if record['returncode'] or len(files)!=1: raise RuntimeError(key)
        shutil.copy2(files[0],args.output/f'{key}_op_summary.csv')
        rows=list(csv.DictReader(files[0].open()))
        chosen=[]
        for op in ['MatMulV2','Add']:
            selected=[r for r in rows if r['OP Type']==op]
            selected.sort(key=lambda r:Decimal(r['Task Start Time(us)'].strip()))
            if len(selected)<16: raise RuntimeError(f'{key} lacks {op}')
            chosen.extend(selected[-16:])
        origin=min(Decimal(r['Task Start Time(us)'].strip()) for r in chosen)
        intervals=[]
        for r in chosen:
            start=float(Decimal(r['Task Start Time(us)'].strip())-origin)
            intervals.append(dict(op=r['OP Type'],stream=r['Stream ID'],task_type=r['Task Type'],
                                  start_us=start,end_us=start+float(r['Task Duration(us)'])))
        events=sorted([(r['start_us'],1,r['op']) for r in intervals]+[(r['end_us'],-1,r['op']) for r in intervals])
        active={'MatMulV2':0,'Add':0};previous=both=union=0.
        for t,d,op in events:
            if all(active.values()): both+=t-previous
            if any(active.values()): union+=t-previous
            active[op]+=d;previous=t
        span=max(r['end_us'] for r in intervals)
        summaries.append(dict(id=key,size=record['size'],mode=record['mode'],kernel_span_us=span,
                              overlap_cube_vector_us=both,kernel_union_us=union,gap_us=span-union,
                              first_vector_us=min(r['start_us'] for r in intervals if r['op']=='Add'),
                              last_matrix_end_us=max(r['end_us'] for r in intervals if r['op']=='MatMulV2')))
        (args.output/f'{key}_intervals.json').write_text(json.dumps(intervals,indent=2)+'\n')
    with (args.output/'summary.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=summaries[0]);w.writeheader();w.writerows(summaries)
    print(json.dumps(summaries,indent=2))


if __name__=='__main__': main()
