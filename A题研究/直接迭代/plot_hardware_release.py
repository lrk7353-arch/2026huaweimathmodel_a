"""Render release curves from exported tables; no solver imports required."""
import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def curve(path):
    rows = list(csv.DictReader(path.open(encoding='utf-8-sig')))
    assert len(rows) == 1500
    result = {}
    for p in (1, 2, 3):
        result[p] = []
        for n in range(1, 6):
            selected = [float(r['speedup']) for r in rows if int(r['problem']) == p and int(r['cores']) == n]
            assert len(selected) == 100
            result[p].append(sum(selected)/100)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--cold', type=Path, required=True)
    p.add_argument('--selected', type=Path, required=True); p.add_argument('--baseline', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True); a = p.parse_args(); a.out.mkdir(parents=True, exist_ok=True)
    cold, selected, baseline = [curve(x) for x in (a.cold, a.selected, a.baseline)]
    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False})
    fig, axes = plt.subplots(1, 3, figsize=(12, 4.5), sharey=True)
    for problem, ax in zip((1, 2, 3), axes):
        ax.plot(range(1, 6), baseline[problem], marker='s', ls=':', color='#89929d', label='Previous selected library')
        ax.plot(range(1, 6), cold[problem], marker='o', color='#3165a8', label='New cold solve')
        ax.plot(range(1, 6), selected[problem], marker='D', color='#be5137', label='New selected library')
        ax.set(title=f'P{problem}', xlabel='Cores', xticks=range(1, 6), ylim=(0, 5.25))
        ax.grid(alpha=.2)
        ax.annotate(f'{cold[problem][-1]:.4f}', (5, cold[problem][-1]), xytext=(-4, -14), textcoords='offset points', ha='right', color='#3165a8')
        ax.annotate(f'{selected[problem][-1]:.4f}', (5, selected[problem][-1]), xytext=(-4, 8), textcoords='offset points', ha='right', color='#be5137')
    axes[0].set_ylabel('Mean speedup (100 graphs)')
    fig.suptitle('Hardware-guided scheduling: cold results and cumulative best solutions', y=.98)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='lower center', bbox_to_anchor=(.5, .075), ncol=3, frameon=False)
    fig.text(.5, .025, 'Cold: B24 per run, mixed CPU hosts; 47 paid host retries and 7 P1 empty-run time extensions are logged.', ha='center', fontsize=9)
    fig.subplots_adjust(left=.065, right=.98, top=.83, bottom=.26, wspace=.16)
    fig.savefig(a.out/'cold_vs_selected.png', dpi=180)
    fig.savefig(a.out/'cold_vs_selected.pdf'); plt.close(fig)
    print(json.dumps(dict(cold=cold, selected=selected), ensure_ascii=False))
