#!/usr/bin/env python3
"""Export the mechanism measurements, keeping device-envelope units explicit."""
import argparse
import csv
from pathlib import Path
import statistics as st
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def main():
    p=argparse.ArgumentParser()
    p.add_argument('full',type=Path)
    p.add_argument('--mixed',type=Path)
    p.add_argument('--mixed-graph',type=Path)
    p.add_argument('--out',type=Path,required=True)
    args=p.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    data=list(csv.DictReader((args.full/'summary.csv').open()))

    def value(suite,mode,**filters):
        found=[r for r in data if r['suite']==suite and r['mode']==mode and
               all(int(r[k])==v for k,v in filters.items())]
        return float(found[0]['median_us']) if len(found)==1 else float('nan')

    fig,axs=plt.subplots(2,3,figsize=(16,9),layout='constrained')
    colors=['#155e75','#d97706','#7c3aed']
    ax=axs[0,0]
    for r,col in zip([1,16,64],colors):
        vals=[value('pipe','pipe',n=1<<22,blocks=1,tile=t,rounds=r,buffers=1)/
              value('pipe','pipe',n=1<<22,blocks=1,tile=t,rounds=r,buffers=2) for t in [512,2048,4096]]
        ax.plot([512,2048,4096],vals,'o-',label=f'{r} vector additions',color=col)
    ax.axhline(1,color='grey',ls='--');ax.set(title='Buffering depends on workload',xlabel='Tile elements (one block)',ylabel='Single / double buffer time');ax.legend(fontsize=8)
    ax=axs[0,1]
    for t,col in zip([1024,4096],colors):
        blocks=[1,2,4,8,16,32,40,48,64]
        vals=[960*16384*12*4/value('bandwidth','pipe',blocks=b,tile=t,rounds=1)/1000 for b in blocks]
        ax.plot(blocks,vals,'o-',label=f'tile={t}',color=col)
    ax.set(title='Concurrency and saturation',xlabel='Launched blocks (not physical cores)',ylabel='Programmed GM bytes / envelope (GB/s)');ax.legend(fontsize=8)
    ax=axs[0,2]
    for g,col in zip([2,4,8],colors):
        vals=[value('barrier','barrier',n=1<<20,blocks=b,groups=g,rounds=64)/
              value('barrier','branch',n=1<<20,blocks=b,groups=g,rounds=64) for b in [1,4,16]]
        ax.plot([1,4,16],vals,'o-',label=f'{g} branches',color=col)
    ax.axhline(1,color='grey',ls='--');ax.set(title='Whole-stage vs branch-local dependency',xlabel='Blocks per branch',ylabel='Whole barrier / local dependency time');ax.legend(fontsize=8)
    ax=axs[1,0]
    for b,col in zip([1,32],colors):
        vals=[value('reuse','materialize',n=1<<20,blocks=b,rounds=1,repeats=k)/
              value('reuse','fused',n=1<<20,blocks=b,rounds=1,repeats=k) for k in [2,8,32]]
        ax.plot([2,8,32],vals,'o-',label=f'{b} blocks',color=col)
    ax.axhline(1,color='grey',ls='--');ax.set(title='On-chip chain vs GM intermediates',xlabel='Chain stages (launch count also changes)',ylabel='Materialized / fused time');ax.legend(fontsize=8)
    ax=axs[1,1]
    g=[1,4,16,64]
    vals=[value('cache','cache_roundrobin',n=1<<22,blocks=32,groups=v)/
          value('cache','cache_adjacent',n=1<<22,blocks=32,groups=v) for v in g]
    ax.plot([32*v for v in g],vals,'o-',color=colors[0]);ax.set_xscale('log',base=2)
    ax.axhline(1,color='grey',ls='--');ax.set(title='Reuse interval / working-set effect',xlabel='Unique input bytes (MiB; output also present)',ylabel='Round-robin / adjacent time')
    ax=axs[1,2]
    for folder,style,name in [(args.mixed,':','eager'),(args.mixed_graph,'-','graph')]:
        if folder and (folder/'raw.csv').exists():
            raw=list(csv.DictReader((folder/'raw.csv').open()))
            for elements,col in zip([1<<20,1<<24],colors):
                vals=[]
                for size in [512,1024,2048]:
                    med={mode:st.median(float(r['device_envelope_us']) for r in raw
                          if int(r['size'])==size and int(r['elements'])==elements and r['mode']==mode)
                         for mode in ['serial','parallel']}
                    vals.append(med['serial']/med['parallel'])
                ax.plot([512,1024,2048],vals,marker='o',ls=style,label=f'{name}, vector {elements*4/2**20:g} MiB',color=col)
            ax.legend(fontsize=7)
    ax.axhline(1,color='grey',ls='--');ax.set(title='Matrix + vector stream scheduling',xlabel='Square matrix dimension',ylabel='Serial / parallel time')
    for ax in axs.flat:
        ax.grid(alpha=.2);ax.spines[['right','top']].set_visible(False)
    fig.suptitle('Ascend 910B3 mechanism probes | median device-event envelopes, 30 samples\nHardware observations, not contest speedups or measured HBM bandwidth',fontsize=14)
    fig.savefig(args.out/'mechanisms.png',dpi=170)
    fig.savefig(args.out/'mechanisms.svg')


if __name__=='__main__':
    main()
