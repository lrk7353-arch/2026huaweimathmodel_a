"""Standalone publication plots for original-task metrics (matplotlib required)."""
import argparse
import csv
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def plot(root):
    with (root/'原题平均曲线.csv').open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.3), constrained_layout=True)
    colors = ['#2378a6', '#cd6a32', '#488554']
    for i in (0, 1):
        part = [r for r in rows if int(r['problem']) == i+1]
        x = [int(r['cores']) for r in part]
        y = [float(r['formal_P1_P2_speedup']) for r in part]
        axes[i].plot(x, y, marker='o', color=colors[i], linewidth=2.3, label='Uniform V2')
        axes[i].plot(x, x, '--', color='#999999', alpha=.7, label='Linear reference')
        axes[i].set_title(f'P{i+1}: mean original-single-core speedup')
        axes[i].set_ylabel('Mean of per-graph time ratios')
        axes[i].legend(frameon=False)
    p3 = [r for r in rows if int(r['problem']) == 3]
    axes[2].plot([int(r['cores']) for r in p3], [float(r['mean_P3_same_core_noL2_over_L2']) for r in p3],
                 marker='o', color=colors[2], linewidth=2.3)
    axes[2].axhline(1, color='#999999', linestyle='--')
    axes[2].set_title('P3: same-core no-L2 / L2 time')
    axes[2].set_ylabel('Independently optimized P2 / P3')
    for axis in axes:
        axis.set_xticks(range(1, 6)); axis.set_xlabel('Number of cores')
        axis.grid(axis='y', alpha=.2); axis.spines[['top','right']].set_visible(False)
    fig.suptitle('100 graphs per point · Frozen uniform search · Official evaluators', fontsize=13)
    for suffix in ('png', 'svg', 'pdf'):
        fig.savefig(root/f'原题平均曲线.{suffix}', dpi=180)
    plt.close(fig)


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('root', type=Path)
    plot(p.parse_args().root)
