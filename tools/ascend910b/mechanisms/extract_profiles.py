#!/usr/bin/env python3
"""Keep original kernel CSVs and derive task overlap / request-weighted L2 counts."""
import argparse
import csv
from decimal import Decimal
import json
from pathlib import Path
import shutil
import statistics as st


def main():
    p=argparse.ArgumentParser()
    p.add_argument('input',type=Path)
    p.add_argument('output',type=Path)
    args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    collection=json.loads((args.input/'collection.json').read_text())
    shutil.copy2(args.input/'collection.json',args.output/'collection.json')
    summaries=[]
    for item in collection:
        key=item['id']; cfg=item['config']; case=args.input/key
        candidates=list(case.glob('prof/PROF_*/mindstudio_profiler_output/op_summary_*.csv'))
        if item['returncode'] or len(candidates)!=1:
            raise RuntimeError(f'Missing or ambiguous profiler output {key}')
        shutil.copy2(candidates[0],args.output/f'{key}_op_summary.csv')
        rows=[r for r in csv.DictReader(candidates[0].open()) if r['Op Name'] in ['vector_probe','sharing_probe']]
        rows.sort(key=lambda r:Decimal(r['Task Start Time(us)'].strip()))
        n,g,k=[int(cfg[v]) for v in ['n','groups','repeats']]
        mode=cfg['mode']
        launches=g*2 if mode in ['branch','barrier'] else g*k if mode.startswith('cache_') else 1 if mode=='fused' else k
        if len(rows)!=3*launches:
            raise RuntimeError(f'Expected 3 sequences for {key}, observed {len(rows)} kernels')
        last=rows[-launches:]
        origin=Decimal(last[0]['Task Start Time(us)'].strip())
        intervals=[]
        for r in last:
            start=float(Decimal(r['Task Start Time(us)'].strip())-origin)
            intervals.append({'stream':r['Stream ID'],'start_us':start,
                              'end_us':start+float(r['Task Duration(us)']),
                              'duration_us':float(r['Task Duration(us)'])})
        events=sorted([(r['start_us'],1) for r in intervals]+[(r['end_us'],-1) for r in intervals])
        active=peak=0;previous=union=0.
        for t,delta in events:
            if active: union+=t-previous
            active+=delta;peak=max(peak,active);previous=t
        span=max(r['end_us'] for r in intervals)
        summary={'id':key,'mode':mode,'last_sequence_launches':launches,
                 'median_kernel_us':st.median(float(r['Task Duration(us)']) for r in last),
                 'kernel_span_us':span,'sum_kernel_us':sum(r['duration_us'] for r in intervals),
                 'kernel_union_us':union,'kernel_gap_us':span-union,'peak_overlapping_kernels':peak,
                 'first_B_start_us':'','last_A_end_us':'','A_B_overlap_window_us':'',
                 'aiv_scalar_ratio_median':'','aiv_vec_ratio_median':'','aiv_mte2_ratio_median':'','aiv_mte3_ratio_median':'',
                 'read_hit_requests':'','read_miss_allocate_requests':'','request_weighted_read_hit_pct':''}
        for metric in ['scalar','vec','mte2','mte3']:
            column=f'aiv_{metric}_ratio'
            if column in last[0]: summary[column+'_median']=st.median(float(r[column]) for r in last)
        if mode in ['barrier','branch']:
            streams={r['stream'] for r in intervals}
            stages=[sorted([r for r in intervals if r['stream']==s],key=lambda r:r['start_us']) for s in streams]
            if any(len(s)!=2 for s in stages): raise RuntimeError(f'Unexpected per-stream sequence {key}')
            end_a=max(s[0]['end_us'] for s in stages)
            start_b=min(s[1]['start_us'] for s in stages)
            summary.update(first_B_start_us=start_b,last_A_end_us=end_a,
                           A_B_overlap_window_us=max(0,end_a-start_b))
            for s in stages:
                s[0]['phase']='A';s[1]['phase']='B'
            (args.output/f'{key}_intervals.json').write_text(json.dumps(intervals,indent=2)+'\n')
        if 'aiv_r0_read_cache_hit' in last[0]:
            hits=sum(float(r[f'aiv_r{c}_read_cache_hit']) for r in last for c in [0,1])
            misses=sum(float(r[f'aiv_r{c}_read_cache_miss_allocate']) for r in last for c in [0,1])
            summary.update(read_hit_requests=hits,read_miss_allocate_requests=misses,
                           request_weighted_read_hit_pct=100*hits/(hits+misses))
        summaries.append(summary)
    with (args.output/'summary.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=summaries[0].keys());w.writeheader();w.writerows(summaries)
    print(json.dumps(summaries,indent=2))


if __name__=='__main__':
    main()
