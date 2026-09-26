"""Rebuild truthful P1 candidate/cold-route figures from saved official records.

No solver or evaluator is imported.  All tested candidates, including regressions
and failed attempts, remain in the exported CSV.  This is a provisional general
research figure layout, not a claim of compliance with any journal template.
"""
import argparse
import csv
import io
import json
import math
import os
from pathlib import Path
import platform
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
from PIL import Image, __version__ as PIL_VERSION


HERE = Path(__file__).resolve().parent
METHODS = ['legacy', 'beam_small', 'cpsat_small', 'beam_large', 'multilevel',
           'cpsat_warm', 'cpsat_intact']
LABELS = {'legacy': 'Legacy joint', 'beam_small': 'Beam, small',
          'cpsat_small': 'CP-SAT, small', 'beam_large': 'Beam, large',
          'multilevel': 'Two-level', 'cpsat_warm': 'Beam + CP-SAT',
          'cpsat_intact': 'CP-SAT, intact'}
# Dark colors plus distinct markers and fixed method rows. Color alone never
# identifies a method; all labels and zero lines are dark on opaque white.
COLORS = ['#333333', '#0072B2', '#B33D00', '#007A5E', '#946027', '#874D8B', '#526B8C']
MARKERS = ['o', 's', '^', 'D', 'v', 'P', 'X']
STYLE = {'font.family': 'DejaVu Sans', 'font.size': 8.5,
         'axes.titlesize': 10, 'axes.labelsize': 9,
         'xtick.labelsize': 8, 'ytick.labelsize': 8.5,
         'axes.spines.top': False, 'axes.spines.right': False,
         'pdf.fonttype': 42, 'ps.fonttype': 42, 'svg.fonttype': 'none',
         'savefig.facecolor': 'white', 'figure.facecolor': 'white',
         'axes.facecolor': 'white', 'text.color': '#222222',
         'axes.labelcolor': '#222222', 'xtick.color': '#222222',
         'ytick.color': '#222222'}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def number(value):
    if value is None or value == '':
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def percent(new, old):
    return 100 * (new / old - 1) if new is not None and old else None


def atomic_text(path, value):
    temp = path.with_name(path.name+'.tmp')
    temp.write_text(value, encoding='utf-8')
    os.replace(temp, path)


def export_csv(path, rows, fields=None):
    fields = fields or list(dict.fromkeys(k for row in rows for k in row))
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(path, '\ufeff'+buffer.getvalue())


def load_warm(root):
    rows, sources, parent_by_case = [], [], {}
    for run in ('run_v1', 'run_v2'):
        source = root/run/'逐次候选.csv'
        if not source.exists():
            raise FileNotFoundError(source)
        sources.append(str(source))
        counters = {}
        for row in read_csv(source):
            case, method = row['case'], row['method']
            if method not in METHODS:
                raise ValueError(f'Unknown method must be explicitly labeled: {method}')
            summary_path = root/run/'arms'/case/method/'summary.json'
            summary = read_json(summary_path)
            if not summary.get('complete', summary.get('completed', False)):
                raise ValueError(f'Warm arm is not complete: {summary_path}')
            parent = tuple(summary['seed_score'])
            if case in parent_by_case and parent_by_case[case] != parent:
                raise ValueError(f'Cannot use a common-parent comparison for {case}')
            parent_by_case[case] = parent
            record = read_json(row['record_path'])
            if record['status'] != row['status']:
                raise ValueError(f'CSV/record status mismatch: {row["record_path"]}')
            metrics = record.get('metrics') or {}
            makespan = metrics.get('makespan') if record['status'] == 'success' else None
            copy_bytes = (metrics.get('data_movement_bytes') or {}).get('added_copy_bytes') if makespan else None
            if makespan is not None and (makespan != number(row['makespan']) or copy_bytes != number(row['added_copy'])):
                raise ValueError(f'CSV/record metrics mismatch: {row["record_path"]}')
            key = case, method
            counters[key] = counters.get(key, 0)+1
            rows.append(dict(run=run, case=case, problem=1, cores=5, method=method,
                candidate_index=counters[key], candidate_name=row['name'], status=record['status'],
                makespan_cycles=makespan, parent_makespan_cycles=parent[0],
                time_change_percent=percent(makespan, parent[0]),
                added_copy_bytes=copy_bytes, parent_added_copy_bytes=parent[1],
                added_copy_change_bytes=copy_bytes-parent[1] if copy_bytes is not None else None,
                added_copy_change_percent=percent(copy_bytes, parent[1]),
                full_plan_proxy=number(row['predicted']), evaluation_seconds=number(row['seconds']),
                candidate_cap=summary['protocol']['max_candidates'],
                generation_cap_seconds=summary['protocol']['generation_seconds'],
                summary_path=str(summary_path), record_path=row['record_path']))
    return rows, sources, parent_by_case


def save_figure(fig, stem):
    info = []
    for fmt in ('pdf', 'png', 'svg'):
        destination = stem.with_suffix('.'+fmt)
        temp = destination.with_name(destination.stem+'.tmp.'+fmt)
        metadata = {'Title': stem.name, 'Creator': 'plot_p1_tonight.py / Matplotlib'}
        if fmt == 'png':
            metadata = {'Title': stem.name, 'Software': 'plot_p1_tonight.py / Matplotlib'}
        fig.savefig(temp, format=fmt, dpi=300, facecolor='white', transparent=False,
                    metadata=metadata)
        os.replace(temp, destination)
        item = dict(path=str(destination), bytes=destination.stat().st_size,
                    width_inches=fig.get_size_inches()[0], height_inches=fig.get_size_inches()[1])
        if fmt == 'png':
            with Image.open(destination) as image:
                item.update(pixels=list(image.size), dpi=list(image.info.get('dpi', [])),
                            mode=image.mode, extrema_alpha=image.getextrema()[-1] if image.mode == 'RGBA' else None)
        elif fmt == 'pdf':
            try:
                from pypdf import PdfReader
                page = PdfReader(destination).pages[0]
                fonts = []
                for value in page['/Resources'].get('/Font', {}).values():
                    font = value.get_object()
                    for descendant in font.get('/DescendantFonts', [font]):
                        descendant = descendant.get_object()
                        descriptor = descendant.get('/FontDescriptor', {})
                        if hasattr(descriptor, 'get_object'):
                            descriptor = descriptor.get_object()
                        fonts.append(dict(subtype=str(descendant.get('/Subtype')),
                                          true_type_embedded='/FontFile2' in descriptor))
                item.update(page_points=list(map(float, page.mediabox)), fonts=fonts)
            except ImportError:
                item['pdf_font_check'] = 'pypdf unavailable; not checked'
        info.append(item)
    return info


def plot_warm(rows, parents, output):
    cases = sorted(parents)
    values = [r['time_change_percent'] for r in rows if r['time_change_percent'] is not None]
    lo = math.floor((min(values+[0])-.1)*4)/4
    hi = math.ceil((max(values+[0])+.1)*4)/4
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, len(cases), figsize=(7.3, 4.7), sharex=True, sharey=True)
        fig.subplots_adjust(left=.20, right=.985, top=.77, bottom=.16, wspace=.10)
        for panel, (ax, case) in enumerate(zip(axes, cases)):
            ax.axvline(0, color='#444444', linestyle='--', linewidth=.85, zorder=0)
            ax.set_xlim(lo, hi)
            ax.set_ylim(len(METHODS)+.05, -.55)
            ax.set_yticks(range(len(METHODS)), [LABELS[m] for m in METHODS])
            ax.xaxis.set_major_locator(MaxNLocator(nbins=4))
            ax.grid(axis='x', color='#E2E2E2', linewidth=.55)
            ax.set_axisbelow(True)
            ax.set_title(f'({chr(97+panel)}) Graph {case.removeprefix("case_")}\n'
                         f'Parent: {parents[case][0]:,} cycles', loc='left', pad=10)
            for method_index, method in enumerate(METHODS):
                group = [r for r in rows if r['case'] == case and r['method'] == method]
                successful = [r for r in group if r['time_change_percent'] is not None]
                for j, row in enumerate(successful):
                    offset = (j-(len(successful)-1)/2)*.25
                    ax.scatter(row['time_change_percent'], method_index+offset,
                               marker=MARKERS[method_index], s=36,
                               color=COLORS[method_index], edgecolor='white', linewidth=.4, zorder=3)
                if successful:
                    best = min(successful, key=lambda r: (r['makespan_cycles'], r['added_copy_bytes']))
                    # Value labels describe the best *actual tested candidate*,
                    # not min(parent,candidate), and all other points remain.
                    x = best['time_change_percent']
                    align = 'left' if x-lo < .4 else 'right' if hi-x < .4 else 'center'
                    ax.annotate(f'{x:+.3f}%', (x, method_index),
                                xytext=(0, -12), textcoords='offset points',
                                ha=align, va='top', fontsize=7.2, color='#222222')
                failures = len(group)-len(successful)
                if failures:
                    ax.text(.98, method_index, f'{failures} failed', transform=ax.get_yaxis_transform(),
                            ha='right', va='center', fontsize=7)
                if not group:
                    ax.text(.5, method_index, 'not run', transform=ax.get_yaxis_transform(),
                            ha='center', va='center', fontsize=7)
            if panel:
                ax.tick_params(axis='y', left=False)
        fig.suptitle('P1 warm-start candidates: all official outcomes', x=.02, y=.97,
                     ha='left', fontsize=12, fontweight='bold')
        fig.text(.02, .895,
                 f'{len(rows)} tested candidates on 3 development graphs, 5 cores. '
                 'Each dot is one candidate; labels show the best tested value.', fontsize=8)
        fig.supxlabel('Makespan change from the common parent (%)   |   negative = faster', y=.065)
        fig.text(.02, .015, 'Shared linear scale. Parent is not substituted for a worse candidate. '
                 'Candidate caps: v1 <= 2 per arm; v2 = 1.', fontsize=7.1)
        output_info = save_figure(fig, output/'warm_all_candidates')
        plt.close(fig)
    return output_info


def load_cold(root):
    source = root/'cold_panel'/'results.csv'
    if not source.exists():
        return [], {'status': 'not_available', 'reason': 'cold_panel/results.csv does not exist'}
    raw = read_csv(source)
    groups = {}
    for row in raw:
        group = groups.setdefault(row['config'], {})
        if row['method'] in group:
            raise ValueError(f'Duplicate cold configuration/method row: {row["config"]}/{row["method"]}')
        group[row['method']] = row
    if len(groups) != 6 or any(set(g) != {'integrated', 'joint_tonight'} for g in groups.values()):
        return [], {'status': 'incomplete', 'reason': 'Expected 6 paired configurations', 'rows': len(raw)}
    if any(row['complete'].lower() != 'true' for row in raw):
        return [], {'status': 'incomplete', 'reason': 'At least one route has not completed', 'rows': len(raw)}
    pairs = []
    for config, group in sorted(groups.items()):
        old, new = group['integrated'], group['joint_tonight']
        a, b = number(old['makespan']), number(new['makespan'])
        ac, bc = number(old['added_copy']), number(new['added_copy'])
        pairs.append(dict(config=config, case=config.split('_p')[0], cores=int(config.rsplit('_n', 1)[1]),
            integrated_makespan_cycles=a, joint_makespan_cycles=b,
            time_change_percent=percent(b, a), integrated_added_copy_bytes=ac,
            joint_added_copy_bytes=bc, added_copy_change_percent=percent(bc, ac),
            added_copy_change_bytes=bc-ac if ac is not None and bc is not None else None,
            integrated_status=old['status'], joint_status=new['status'],
            integrated_calls=old['calls'], joint_calls=new['calls'],
            integrated_failed=old['failed'], joint_failed=new['failed'],
            integrated_elapsed_seconds=old['elapsed_seconds'], joint_elapsed_seconds=new['elapsed_seconds'],
            integrated_stop_reason=old['stop_reason'], joint_stop_reason=new['stop_reason'],
            integrated_summary_path=old['summary_path'], joint_summary_path=new['summary_path']))
    return pairs, {'status': 'ready', 'source': str(source), 'rows': len(raw),
                   'configurations': len(pairs), 'distinct_graphs': len({r['case'] for r in pairs})}


def plot_cold(pairs, output):
    finite = [r['time_change_percent'] for r in pairs if r['time_change_percent'] is not None]
    if not finite:
        return []
    margin = max(.15, (max(finite+[0])-min(finite+[0]))*.12)
    limits = min(finite+[0])-margin, max(finite+[0])+margin
    grouped = [[r for r in pairs if r['cores'] == 5], [r for r in pairs if r['cores'] != 5]]
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(7.3, 3.7), sharex=True,
                                 gridspec_kw={'width_ratios': [1, 1]})
        fig.subplots_adjust(left=.15, right=.98, top=.76, bottom=.20, wspace=.40)
        for index, (ax, group) in enumerate(zip(axes, grouped)):
            ax.axvline(0, color='#444444', linestyle='--', linewidth=.9)
            ax.set_xlim(*limits)
            ax.set_ylim(len(group)-.4, -.6)
            ax.set_yticks(range(len(group)), [r['case'].removeprefix('case_')+f' / n{r["cores"]}' for r in group])
            ax.set_title('5-core configurations' if index == 0 else 'Lower-core configurations', loc='left')
            ax.xaxis.set_major_locator(MaxNLocator(nbins=4))
            ax.grid(axis='x', color='#E2E2E2', linewidth=.55)
            for y, row in enumerate(group):
                value = row['time_change_percent']
                if value is None:
                    ax.text(.5, y, 'no valid pair', transform=ax.get_yaxis_transform(), ha='center')
                else:
                    ax.scatter(value, y, marker='s' if index == 0 else 'D', s=42,
                               color='#0072B2' if index == 0 else '#874D8B', zorder=3)
                    ax.annotate(f'{value:+.3f}%', (value, y), xytext=(0, -12),
                                textcoords='offset points', ha='center', fontsize=8)
        fig.suptitle('P1 complete cold-start routes: joint vs integrated', x=.02, y=.97,
                     ha='left', fontsize=12, fontweight='bold')
        fig.text(.02, .88, f'{len(pairs)} paired configurations; '
                 f'{len({r["case"] for r in pairs})} distinct graphs. All configuration outcomes retained.', fontsize=8)
        fig.supxlabel('Makespan change: 100 x (joint / integrated - 1) (%)', y=.09)
        fig.text(.02, .025, 'Negative = joint route faster. Configurations are not independent graph replicates.', fontsize=7.4)
        info = save_figure(fig, output/'cold_complete_routes')
        plt.close(fig)
    return info


def notes(rows, parents, cold):
    count = len(rows)
    failures = sum(r['status'] != 'success' for r in rows)
    baseline = '\n'.join(f'- {case}：{values[0]} 周期，额外 COPY {values[1]} 字节。'
                         for case, values in sorted(parents.items()))
    return f'''# 图表生成说明与图注

这些是供竞赛研究报告使用的通用图表，尚未针对特定期刊版式验证。没有新增官方评测，没有修改原始记录。

## 温启动全候选图

`warm_all_candidates.pdf/png/svg`：三个开发图分别为一个面板，全部是 P1、5 核。共 {count} 条候选评测，失败 {failures} 条。每个点是一份真实候选，正值为退步，负值为耗时减少；没有用父方案替换失败的优化，从而把退步画成零。每种方法旁的数字是其实际测试候选中的最小耗时对应变化，其他候选仍全部保留。点的轻微纵向分离仅为避免重叠，不改变横轴数值。三面板共用线性坐标范围，零线即共同父方案。

归一化公式：`100 × (候选 makespan / 本图共同父方案 makespan − 1)`。每个图的 run_v1、run_v2 父方案一致性由脚本逐项检查。共同父方案为：

{baseline}

run_v1 每个图每种方法最多 2 次候选评测（去重后可能只有 1 次），run_v2 的 `Beam + CP-SAT` 和 `CP-SAT, intact` 各只有 1 次。不能把候选数量不同的最好值解释为严格等调用数的算法总排名。每臂生成上限20秒，不代表实际耗时相等。只展示3张开发图，候选不是独立重复，不添加置信区间或显著性标记，也不宣称100图总体优劣。

`Beam + CP-SAT` 是 beam 强起点接 CP-SAT 的组合方法；`CP-SAT, intact` 保留既有 Task 划分并调整局部核心/顺序。二者都不是整图自由划分的全局最优求解器。`Legacy joint` 是旧联合细化（含phase与tensor-cap），并非每个候选都属于phase。大小区域、两层方法的完整实现与预算应配合实验报告解释。

替代文字：三个图的七种方法真实候选耗时变化点图。047上多种候选改善，075大多退步，085仅少数方案改善；所有正向退步点均保留。精确值与时间/COPY权衡见 `warm_time_copy.csv`。

## 时间与 COPY 数据

`warm_time_copy.csv` 保留原始周期数、额外COPY字节、共同父方案、变化量、百分比、候选名称、预算与官方记录路径。COPY变化使用额外搬运字节，而非原图必要搬运总量；分母为零时百分比留空，不以0代替。更快但COPY增加不能称为两指标全面占优。

## 完整冷启动路线图

当前状态：`{cold['status']}`；{cold.get('reason', '已读取6配置、两路线完整结果。')}

只有 `cold_panel/results.csv` 存在、包含6配置的12条配对路线且全部完成时，才输出 `cold_complete_routes.pdf/png/svg` 与 `cold_time_copy.csv`。图中5核与低核配置分面，显示 `100 × (joint_tonight / integrated − 1)`，包括新路线较差的配置；缺失有效方案标记为 no valid pair，不补成零。六个配置不是六次独立图重复，图注另列不同图的数量。温启动强解改进与冷启动完整路线比较分开，不混用。

## 复现、导出与工具来源

运行 `plot_p1_tonight.py --root P1多尺度联合优化_20260926` 会从原始CSV和官方 record.json 核对数值并重建输出；不会导入求解器或调用官方评测器。PDF与SVG为矢量文本/点图，PNG按最终7.3英寸宽度直接渲染为300 dpi。白色不透明背景；方法同时用名称、颜色与形状区分。SVG文本可编辑，因此显示依赖接收电脑的字体。

本次使用本机既有 Matplotlib/Pillow 环境，无新增安装。格式、像素、版本及数据来源见 `figure_manifest.json`。这些检查不构成期刊合规或完整无障碍认证。

绘图过程采用 `scientific-visualization` 技能中的原始数据保留、共同坐标、负例展示与颜色冗余编码规范。以下仅为绘图工具来源，不作为本题调度算法的学术依据：Timothy Kassis, Vinayak Agarwal, Yuhuan He, Darshil Patel, Aubrey M. Brueckner (2026), *Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents*, https://doi.org/10.48550/arXiv.2609.00065 。作者、年份和最新版本已于2026-09-26核对 arXiv 官方记录：https://arxiv.org/abs/2609.00065 （最新修订日期2026-09-02）。
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=HERE/'P1多尺度联合优化_20260926')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    output = (args.output or root/'论文证据'/'图表').resolve()
    output.mkdir(parents=True, exist_ok=True)
    rows, sources, parents = load_warm(root)
    export_csv(output/'warm_time_copy.csv', rows)
    files = plot_warm(rows, parents, output)
    cold_rows, cold = load_cold(root)
    if cold_rows:
        export_csv(output/'cold_time_copy.csv', cold_rows)
        files.extend(plot_cold(cold_rows, output))
    def contrast(hex_color):
        rgb = [int(hex_color[i:i+2], 16)/255 for i in (1, 3, 5)]
        linear = [x/12.92 if x <= .04045 else ((x+.055)/1.055)**2.4 for x in rgb]
        luminance = sum(w*x for w, x in zip((.2126, .7152, .0722), linear))
        return 1.05/(luminance+.05)
    manifest = dict(python=sys.version, python_executable=sys.executable,
        platform=platform.platform(), matplotlib=matplotlib.__version__, pillow=PIL_VERSION,
        candidate_rows=len(rows), success_rows=sum(r['status']=='success' for r in rows),
        source_tables=sources, official_records_checked=True, cold_status=cold,
        normalization='100 * (actual candidate / same-case parent - 1)',
        error_bars='none; candidates are not independent replicates',
        exclusions='none; failed attempts retained in CSV and explicitly marked if present',
        palette_contrast_on_white={color: contrast(color) for color in COLORS},
        text_contrast_on_white=contrast('#222222'),
        contrast_caveat='Marker shape and direct method labels provide redundant identity; ratios do not certify accessibility.',
        outputs=files)
    atomic_text(output/'figure_manifest.json', json.dumps(manifest, indent=2, ensure_ascii=False)+'\n')
    atomic_text(output/'图表生成说明.md', notes(rows, parents, cold))
    print(json.dumps(dict(output=str(output), warm_candidates=len(rows),
                          cold=cold, files=[f['path'] for f in files]), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
