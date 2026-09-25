#!/usr/bin/env python3
"""Summarize measured envelopes and paired variants without claiming kernel time."""
import argparse
import csv
import json
from pathlib import Path
import random
import statistics as st


def q(values, p):
    v = sorted(values)
    i = (len(v)-1)*p
    lo = int(i)
    return v[lo] + (v[min(lo+1,len(v)-1)]-v[lo])*(i-lo)


def write_csv(path, rows):
    if not rows:
        return
    with path.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('directory', type=Path)
    args = p.parse_args()
    root = args.directory
    all_stats, pairs = [], []
    for file in sorted(root.glob('*_config.csv')):
        suite = file.name.removesuffix('_config.csv')
        raw_path = root / f'{suite}_raw.csv'
        if not raw_path.exists():
            continue
        configs = {r['id']: r for r in csv.DictReader(file.open())}
        raw = {key: {} for key in configs}
        for row in csv.DictReader(raw_path.open()):
            if float(row['max_abs_error']) != 0:
                raise RuntimeError(f'Invalid numeric result {row}')
            raw[row['id']][int(row['sample'])] = row
        stats = {}
        for key, c in configs.items():
            samples = list(raw[key].values())
            if not samples:
                continue
            dev = [float(r['device_envelope_us']) for r in samples]
            host = [float(r['host_us']) for r in samples]
            n, rounds, groups, repeats = [int(c[k]) for k in ['n','rounds','groups','repeats']]
            mode = c['mode']
            launches = (groups*2 if mode in ['branch','barrier'] else groups*repeats
                        if mode.startswith('cache_') else 1 if mode=='fused' else repeats)
            # Programmed GM traffic, not hardware HBM traffic: L2 may serve reads/writes.
            gm_bytes = launches*n*12
            row = {'suite':suite, **c, 'samples':len(samples), 'median_us':st.median(dev),
                   'p10_us':q(dev,.1),'p90_us':q(dev,.9),'host_median_us':st.median(host),
                   'launches':launches, 'programmed_gm_bytes':gm_bytes,
                   'logical_GBps':gm_bytes/st.median(dev)/1000,
                   'queue_buffer_bytes_per_block':int(c['buffers'])*int(c['tile'])*12,
                   'read_workset_bytes':n*groups*8, 'all_outputs_correct':True}
            stats[key]=row; all_stats.append(row)
        if suite not in ['pipe','barrier','reuse','cache']:
            continue
        paired = {}
        for key,c in configs.items():
            excluded = {'id','buffers'} if suite=='pipe' else {'id','mode'}
            group = tuple((k,v) for k,v in c.items() if k not in excluded)
            paired.setdefault(group, []).append(key)
        for ids in paired.values():
            if len(ids)!=2 or not all(k in stats for k in ids):
                continue
            base = next(k for k in ids if configs[k]['buffers']=='1') if suite=='pipe' else next(
                k for k in ids if configs[k]['mode'] in ['barrier','materialize','cache_roundrobin'])
            new = next(k for k in ids if k!=base)
            reps = sorted(set(raw[base]) & set(raw[new]))
            ratios = [float(raw[base][r]['device_envelope_us'])/float(raw[new][r]['device_envelope_us']) for r in reps]
            rng = random.Random(20260926)
            bootstrap = [st.median(rng.choices(ratios,k=len(ratios))) for _ in range(1000)]
            pairs.append({'suite':suite,'baseline':base,'candidate':new, 'paired_samples':len(reps),
                          'baseline_us':stats[base]['median_us'],'candidate_us':stats[new]['median_us'],
                          'ratio_of_medians':stats[base]['median_us']/stats[new]['median_us'],
                          'paired_ratio_median':st.median(ratios),
                          'paired_ratio_ci_low':q(bootstrap,.025),'paired_ratio_ci_high':q(bootstrap,.975),
                          'candidate_faster_samples':sum(r>1 for r in ratios)})
    write_csv(root/'summary.csv',all_stats)
    write_csv(root/'pairs.csv',pairs)
    overview = {}
    for suite in sorted({r['suite'] for r in all_stats}):
        sr = [r for r in all_stats if r['suite']==suite]
        sp = [r for r in pairs if r['suite']==suite]
        overview[suite]={'configs':len(sr),'samples':sum(r['samples'] for r in sr),
                         'pairs':len(sp),'pairs_over_5pct':sum(r['paired_ratio_ci_low']>1.05 for r in sp),
                         'pairs_under_minus5pct':sum(r['paired_ratio_ci_high']<1/1.05 for r in sp)}
        if sp:
            overview[suite]['median_pair_ratio']=st.median(r['ratio_of_medians'] for r in sp)
            overview[suite]['best']=max(sp,key=lambda r:r['ratio_of_medians'])
            overview[suite]['worst']=min(sp,key=lambda r:r['ratio_of_medians'])
    (root/'overview.json').write_text(json.dumps(overview,indent=2)+'\n')
    print(json.dumps(overview,indent=2))


if __name__=='__main__':
    main()
