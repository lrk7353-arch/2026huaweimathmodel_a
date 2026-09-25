#!/usr/bin/env python3
"""Plot the causal checks: actual overlap, PMU counts, sharing and mixed pipelines."""
import argparse
import csv
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch


def main():
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);args=p.parse_args()
    root=args.root
    fig,axes=plt.subplots(2,2,figsize=(14,9),layout='constrained')
    colors={'A':'#d97706','B':'#155e75','MatMulV2':'#d97706','Add':'#155e75'}
    ax=axes[0,0]
    for i,(key,name) in enumerate([('barrier_038','Whole barrier'),('barrier_039','Branch local')]):
        intervals=json.loads((root/'profile_evidence'/f'{key}_intervals.json').read_text())
        streams=sorted({r['stream'] for r in intervals})
        for r in intervals:
            y=i*3+streams.index(r['stream'])
            ax.broken_barh([(r['start_us']/1000,(r['end_us']-r['start_us'])/1000)],(y,.65),color=colors[r['phase']])
        ax.text(0,i*3+1.9,name,fontsize=10)
    ax.set(xlabel='Time since first kernel (ms)',title='A/B overlap appears after removing the whole barrier',yticks=[],ylim=(-.25,6.3))
    ax.legend(handles=[Patch(color=colors[k],label=f'Phase {k}') for k in ['A','B']],loc='upper right',fontsize=8)
    ax=axes[0,1]
    prof={r['id']:r for r in csv.DictReader((root/'profile_evidence/summary.csv').open())}
    for ids,name,color in [(['cache_043','cache_042'],'Input pool 128 MiB','#7c3aed'),
                            (['cache_047','cache_046'],'Input pool 2048 MiB','#155e75')]:
        x=[float(prof[k]['request_weighted_read_hit_pct']) for k in ids]
        y=[float(prof[k]['median_kernel_us']) for k in ids]
        ax.plot(x,y,'o-',color=color,label=name)
        for k,xx,yy in zip(ids,x,y):
            adjacent=prof[k]['mode']=='cache_adjacent'
            offset=(0,9) if name.endswith('2048 MiB') else ((-2,12) if adjacent else (-2,-14))
            ax.annotate('adjacent' if adjacent else 'round-robin',(xx,yy),
                        xytext=offset,textcoords='offset points',fontsize=8,ha='right' if xx>90 else 'left')
    ax.set(xlabel='PMU read-request hit percentage',ylabel='Median profiled kernel duration (us)',title='Higher hit rate is useful only when it removes a bottleneck',xlim=(-5,105),ylim=(16,50));ax.legend(fontsize=8,loc='center right')
    ax=axes[1,0]
    stats=list(csv.DictReader((root/'sharing_01/summary.csv').open()))
    for n,color in [(31<<18,'#d97706'),(31<<20,'#155e75')]:
        vals=[]
        for b in [1,4,16,32]:
            times={r['mode']:float(r['median_us']) for r in stats if int(r['n'])==n and int(r['blocks'])==b
                   and int(r['rounds'])==1 and int(r['tile'])==4096}
            vals.append(times['private']/times['shared'])
        ax.plot([1,4,16,32],vals,'o-',color=color,label=f'{n*4/2**20:g} MiB per private input')
    ax.axhline(1,color='grey',ls='--');ax.set(xlabel='Launched vector blocks',ylabel='Private copies / shared input time',title='Cross-block reuse helps only some resource regimes');ax.legend(fontsize=8)
    ax=axes[1,1]
    for i,(key,name) in enumerate([('m1024_serial','Serial graph replay'),('m1024_parallel','Parallel graph replay')]):
        rows=json.loads((root/'mixed_profile_evidence'/f'{key}_intervals.json').read_text())
        for row in rows:
            y=i*3+int(row['op']=='Add')
            ax.broken_barh([(row['start_us'],row['end_us']-row['start_us'])],(y,.65),color=colors[row['op']])
        ax.text(0,i*3+1.9,name,fontsize=10)
    ax.set(xlabel='Time since first selected kernel (us)',yticks=[],title='Measured Cube / Vector overlap, not just two host streams',ylim=(-.25,6.3))
    ax.legend(handles=[Patch(color=colors[k],label=k) for k in ['MatMulV2','Add']],loc='upper right',fontsize=8)
    for ax in axes.flat:
        ax.grid(alpha=.18);ax.spines[['right','top']].set_visible(False)
    fig.suptitle('Ascend 910B3 | profiler evidence and controlled sharing\nTimelines and PMU observations are separate from the unprofiled measurements',fontsize=14)
    out=root/'figures_final';out.mkdir(exist_ok=True)
    fig.savefig(out/'causal_evidence.png',dpi=170);fig.savefig(out/'causal_evidence.svg')


if __name__=='__main__':main()
