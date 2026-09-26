"""Scientific figure from existing official metrics; matplotlib only for plotting."""
import csv
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
summary = json.loads((HERE / '分析摘要.json').read_text())
micros = {r['name']:r for r in csv.DictReader((HERE / '既有微实验正负例.csv').open(encoding='utf-8-sig'))}
plt.rcParams.update({'svg.hashsalt':'p3-capacity-analysis', 'font.size':11, 'axes.spines.top':False, 'axes.spines.right':False})
fig, ax = plt.subplots(1, 2, figsize=(11.4, 4.7), gridspec_kw={'width_ratios':[1.25, 1]})
cats = ['first_miss_bytes', 'pre_first_insert_repeat_miss_bytes', 'post_eviction_miss_bytes']
labels = ['First read', 'Repeat before\nfirst insertion', 'Read after eviction']
values = [summary['totals'][k] / 1e6 for k in cats]
ax[0].barh(labels, values, color=['#426fa5', '#d39737', '#8564a1'], height=.56)
for i, (v,k) in enumerate(zip(values,cats)):
    ax[0].text(v+5, i, f'{v:.2f} MB  ({summary["miss_byte_fractions"][k]*100:.2f}%)', va='center', fontsize=9)
ax[0].set_xlim(0, 505)
ax[0].invert_yaxis()
ax[0].set(xlabel='Miss traffic, MB = 1,000,000 bytes', title='A. Saved five-core P3 traces (100 graphs)')
deltas=[]
for prefix in ['short_compute','long_follower_compute']:
    a=int(micros[prefix+'_simultaneous']['makespan'])
    b=int(micros[prefix+'_staggered']['makespan'])
    deltas.append((b/a-1)*100)
ax[1].bar(['Short compute','Long compute'], deltas, color=['#238b77','#bd5b42'], width=.5)
ax[1].axhline(0,color='#667078',linewidth=1)
for i,v in enumerate(deltas):
    ax[1].text(i,v-1 if v<0 else v+1,f'{v:+.2f}%',ha='center',va='top'if v<0 else'bottom')
ax[1].set(ylim=(-31,9), ylabel='Makespan change (%) — lower is better', title='B. Same hit-rate increase, opposite outcomes')
ax[1].text(.5,5.5,'Both pairs: byte hit rate 0% → 33.33%',ha='center',fontsize=9)
for a in ax:
    a.grid(axis='x' if a is ax[0] else 'y',alpha=.18)
    a.set_axisbelow(True)
fig.text(.05,.025,'A: descriptive miss histories, not recoverable time or a capacity sweep. B: two separate synthetic graphs;\nwithin each pair only legal subgraph order changes. Official configuration and plans were not re-evaluated.',fontsize=9,color='#42484d')
fig.tight_layout(rect=(0,.13,1,1))
for ext in ['png','svg']:
    target = HERE / ('容量时序与命中率反例.'+ext)
    metadata = {'Date': None} if ext == 'svg' else {}
    fig.savefig(target, dpi=180, metadata=metadata)
    if ext == 'svg':
        target.write_text('\n'.join(line.rstrip() for line in target.read_text(encoding='utf-8').splitlines()) + '\n', encoding='utf-8')
