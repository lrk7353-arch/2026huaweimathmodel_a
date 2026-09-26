"""Plot published cumulative curves, keeping this scope distinct from cold runs."""
import csv
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

OUT = Path(__file__).resolve().parents[1] / '最终精选1500'
rows = list(csv.DictReader((OUT / '题目口径核数曲线.csv').open(encoding='utf-8-sig')))
cache = list(csv.DictReader((OUT / 'P3同核数汇总.csv').open(encoding='utf-8-sig')))
plt.rcParams.update({'font.size': 11, 'axes.spines.top': False, 'axes.spines.right': False})
fig, axes = plt.subplots(1, 2, figsize=(11.8, 4.8), gridspec_kw={'width_ratios': [1.3, 1]})
for p, color in [(1, '#2463a4'), (2, '#188879'), (3, '#b85921')]:
    rs = [r for r in rows if int(r['problem']) == p]
    axes[0].plot([int(r['cores']) for r in rs], [float(r['mean_speedup']) for r in rs], '-o', color=color, label=f'P{p}')
axes[0].set(xlabel='Number of cores', ylabel='Mean speedup (100 graphs)', xticks=range(1, 6), ylim=(.8, 5.15), title='Cumulative selected library')
axes[0].legend(frameon=False)
axes[1].plot([int(r['cores']) for r in cache], [float(r['mean_P2_over_P3']) for r in cache], '-o', color='#8c5197')
axes[1].axhline(1, color='#899198', linewidth=1, linestyle='--')
axes[1].set(xlabel='Number of cores', ylabel='Mean of per-graph T(P2) / T(P3)', xticks=range(1, 6), title='Same-core P2 / P3 comparison')
for ax in axes:
    ax.grid(axis='y', alpha=.2)
fig.text(.08, .035, '1,500 archived plans; single-core speedup is 1. P2/P3 plans are optimized separately.\nCumulative results include historical search; they are not a new fixed-budget cold run.', fontsize=9, color='#41484d')
fig.tight_layout(rect=(0, .12, 1, 1))
for ext in ('png', 'pdf'):
    fig.savefig(OUT / ('累计加速比曲线.' + ext), dpi=180)
