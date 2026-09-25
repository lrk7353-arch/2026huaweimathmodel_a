"""Export clearly labelled cumulative curves (requires matplotlib)."""
import csv
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
HERE=Path(__file__).resolve().parents[1]
rows=list(csv.DictReader((HERE/'累计核数曲线.csv').open(encoding='utf-8-sig')))
fig,axes=plt.subplots(1,2,figsize=(10.2,4),layout='constrained')
colors={1:'#b14737',2:'#246d95',3:'#278270'}
for p in (1,2,3):
    rs=sorted((r for r in rows if int(r['problem'])==p),key=lambda r:int(r['cores']))
    xs=[int(r['cores']) for r in rs]
    for ax,key in zip(axes,('mean_speedup','optimized_singlecore_normalized_speedup')):
        ax.plot(xs,[float(r[key]) for r in rs],marker='o',lw=2,color=colors[p],label=f'P{p}')
for ax,title in zip(axes,('Fixed original single-core reference','Selected same-scenario single-core reference')):
    ax.set(title=title,xlabel='Number of cores',ylabel='Mean per-graph speedup',xticks=range(1,6),ylim=(.8,5.2))
    ax.grid(axis='y',alpha=.2);ax.legend(frameon=False)
    ax.spines[['right','top']].set_visible(False)
fig.suptitle('Cumulative selected library: 100 graphs, not a cold-start solver score',fontsize=12)
fig.savefig(HERE/'累计加速比曲线.png',dpi=180)
fig.savefig(HERE/'累计加速比曲线.pdf')

ledger=list(csv.DictReader((HERE/'累计1500配置成绩.csv').open(encoding='utf-8-sig')))
lookup={(r['case'],int(r['problem']),int(r['cores'])):int(r['makespan']) for r in ledger}
cache=[]
for n in range(1,6):
    ratios=[lookup[f'case_{i:03d}',2,n]/lookup[f'case_{i:03d}',3,n] for i in range(1,101)]
    cache.append(dict(cores=n,mean_optimized_P2_over_P3=sum(ratios)/100,
        scope='Separately optimized P2/P3 selected plans; not same-plan cache-only attribution.'))
with (HERE/'同核P2P3累计对照.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(cache[0]));w.writeheader();w.writerows(cache)
