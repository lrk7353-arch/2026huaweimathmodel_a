"""Paper figures from frozen observations; no solver or official evaluator imports."""
from datetime import datetime, timezone
import csv
import hashlib
import json
from pathlib import Path
import platform
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.ticker import FixedLocator, FixedFormatter, NullFormatter
import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
OUT = HERE / "figures_v2"
FONT = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
SOURCES = {
    "all100": HERE / "第二轮独立核验/campaign_aggregate.json",
    "four_cells": HERE / "P3四格_v2/summary.json",
    "control071": HERE / "P3四格_v2/control071_ablation.json",
    "old_cache071": RESEARCH / "advanced_solver/runs/cache_refine_v1/case_071/summary.json",
    "old_cache071_candidates": RESEARCH / "advanced_solver/runs/cache_refine_v1/case_071/all_evaluations.json",
}
BLUE, ORANGE, GREEN, GRAY = "#0072B2", "#D55E00", "#009E73", "#555555"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_csv(name, rows):
    with (OUT / name).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def export(fig, name, outputs):
    png, pdf = OUT / (name + ".png"), OUT / (name + ".pdf")
    fig.savefig(png, dpi=300, facecolor="white", transparent=False)
    # Remove an entirely opaque alpha channel without changing any RGB pixels.
    with Image.open(png) as image:
        if image.mode == "RGBA":
            assert image.getextrema()[-1] == (255, 255)
            rgb = image.convert("RGB")
            rgb.save(png, dpi=(300, 300))
    fig.savefig(pdf, facecolor="white", transparent=False,
                metadata={"Title": name, "Author": "A problem research", "Subject": "Frozen observed results; no confidence intervals"})
    with Image.open(png) as image:
        meta = {"pixels": list(image.size), "mode": image.mode, "dpi": list(image.info.get("dpi", []))}
    outputs.append({"name": name, "png": str(png), "pdf": str(pdf), "width_mm": fig.get_figwidth() * 25.4,
                    "height_mm": fig.get_figheight() * 25.4, "raster_metadata": meta,
                    "png_sha256": sha(png), "pdf_sha256": sha(pdf),
                    "all_artists_vector_in_pdf": True, "bbox_inches": None})
    plt.close(fig)


def simple_axis(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["bottom", "left"]].set_color("#666666")
    ax.tick_params(length=3, color="#666666", labelsize=8)


def table(ax, columns, rows, *, bbox, widths=None, font_size=8.3):
    result = ax.table(cellText=rows, colLabels=columns, colWidths=widths,
                      cellLoc="center", colLoc="center", bbox=bbox)
    result.auto_set_font_size(False)
    result.set_fontsize(font_size)
    for (row, col), cell in result.get_celld().items():
        cell.set_linewidth(0.5)
        cell.set_edgecolor("#D0D0D0")
        cell.set_facecolor("#F0F3F5" if row == 0 else "white")
        cell.PAD = 0.03
        if row == 0:
            cell.get_text().set_color("#111111")
    return result


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    before = {key: {"path": str(path), "sha256": sha(path)} for key, path in SOURCES.items()}
    all100, four, control, old, old_candidates = (read(SOURCES[key]) for key in SOURCES)
    assert all100["case_count"] == 100 and len(all100["rows"]) == 100
    rows = sorted(all100["rows"], key=lambda row: row["case"])
    assert [row["case"] for row in rows] == ["case_{:03d}".format(i) for i in range(1, 101)]
    assert all(row["best_new"] is not None and min(row["control"], row["best_new"]) == row["selected"] for row in rows)
    control_time = np.array([row["control"] for row in rows], float)
    bare_time = np.array([row["best_new"] for row in rows], float)
    retained_time = np.array([row["selected"] for row in rows], float)
    baseline = np.array([row["singlecore"] for row in rows], float)
    wins = int(np.sum(bare_time < control_time)); ties = int(np.sum(bare_time == control_time)); losses = int(np.sum(bare_time > control_time))
    assert (wins, ties, losses) == (44, 5, 51)
    means = [float(np.mean(baseline / values)) for values in (control_time, bare_time, retained_time)]
    assert abs(means[0] - all100["official_mean_singlecore_over_control"]) < 1e-12
    assert abs(means[1] - all100["best_new_vs_control"]["official_mean_singlecore_over_time"]) < 1e-12
    assert abs(means[2] - all100["retained_vs_control"]["official_mean_singlecore_over_time"]) < 1e-12
    csv_rows = [{**row, "bare_time_over_control": row["best_new"] / row["control"],
                 "retained_time_over_control": row["selected"] / row["control"],
                 "official_control_speedup": row["singlecore"] / row["control"],
                 "official_bare_speedup": row["singlecore"] / row["best_new"],
                 "official_retained_speedup": row["singlecore"] / row["selected"]} for row in rows]
    write_csv("figure1_all100_data.csv", csv_rows)
    write_csv("figure1_aggregate_data.csv", [
        {"method": name, "official_arithmetic_mean_B_over_T": mean, "n_graphs": 100,
         "wins_against_control": w, "ties_against_control": t, "losses_against_control": l}
        for name, mean, w, t, l in zip(("WCC_control", "bare_operation", "retained_portfolio"), means, (0, 44, 44), (100, 5, 56), (0, 51, 0))])

    mechanism = []
    for case in four["cases"]:
        cells = case["cells"]
        assert all(record["status"] == "success" for record in cells.values())
        values = {label: record["metrics"]["makespan"] for label, record in cells.items()}
        h, s, r = values["t2_pi2"] / values["t3_pi2"], values["t3_pi2"] / values["t3_pi3"], values["t2_pi2"] / values["t3_pi3"]
        assert abs(h * s - r) < 1e-12
        mechanism.append({"case": case["case"], **values, "H_hardware": h, "S_policy": s, "R_total": r,
                          "H_minus1_pct": (h - 1) * 100, "S_minus1_pct": (s - 1) * 100, "R_minus1_pct": (r - 1) * 100,
                          "policy_status": case["policy_status"], "pi2_plan_sha256": case["pi2_plan_sha256"],
                          "pi3_plan_sha256": case["pi3_plan_sha256"],
                          "pi2_added_copy_bytes": cells["t2_pi2"]["metrics"]["data_movement_bytes"]["added_copy_bytes"],
                          "pi3_added_copy_bytes": cells["t2_pi3"]["metrics"]["data_movement_bytes"]["added_copy_bytes"],
                          "pi2_P3_byte_hit_rate": cells["t3_pi2"]["metrics"]["cache_stats"]["hit_rate"],
                          "pi3_P3_byte_hit_rate": cells["t3_pi3"]["metrics"]["cache_stats"]["hit_rate"]})
    assert len(mechanism) == 10 and [row["case"] for row in mechanism] == ["case_" + x for x in ("011", "019", "044", "049", "051", "064", "069", "071", "082", "093")]
    write_csv("figure2_four_cell_data.csv", mechanism)
    ablation_values = [row["record"]["metrics"]["makespan"] for row in control["calls"]]
    assert ablation_values == [8190, 8234, 8246]
    assert control["strict_final_makespan"] == 8190
    assert old["source_record"]["hashes"]["plan_sha256"] == control["calls"][0]["record"]["hashes"]["plan_sha256"]
    round0 = min((r for r in old_candidates if r["round"] == 0 and r["record"]["status"] == "success"), key=lambda r: r["record"]["metrics"]["makespan"])
    round1 = min((r for r in old_candidates if r["round"] == 1 and r["record"]["status"] == "success"), key=lambda r: r["record"]["metrics"]["makespan"])
    full_values = [old["source_p3_makespan"], round0["record"]["metrics"]["makespan"], round1["record"]["metrics"]["makespan"]]
    assert full_values == [8190, 8189, 7952] and round1["metadata"]["is_reencoding_control"]
    ablation_csv = []
    for method, values, semantics in (("forced_observed_reencoding", ablation_values, "candidate value; forced continuation after regression"),
                                     ("strict_retention_control", [8190, 8190, 8190], "incumbent; second step derived from identical evaluated plan hash"),
                                     ("old_full_cache_probe", full_values, "retained incumbent; final winner is reencoding control")):
        for stage, value in enumerate(values):
            ablation_csv.append({"case": "case_071", "method": method, "stage": stage, "makespan_cycles": value, "semantics": semantics})
    write_csv("figure2_control071_data.csv", ablation_csv)

    font_manager.fontManager.addfont(str(FONT))
    font_name = font_manager.FontProperties(fname=str(FONT)).get_name()
    style = {"font.family": font_name, "font.size": 8.6, "axes.titlesize": 9.2,
             "axes.labelsize": 8.4, "legend.fontsize": 7.5, "axes.unicode_minus": False,
             "pdf.fonttype": 42, "ps.fonttype": 42, "figure.facecolor": "white", "axes.facecolor": "white",
             "text.color": "#161616", "axes.labelcolor": "#222222", "savefig.facecolor": "white"}
    outputs = []
    with plt.rc_context(style):
        fig = plt.figure(figsize=(180 / 25.4, 126 / 25.4))
        gs = fig.add_gridspec(1, 2, left=0.09, right=0.98, bottom=0.22, top=0.78, width_ratios=[1.9, 1], wspace=0.27)
        ax, aggregate = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])
        simple_axis(ax); simple_axis(aggregate)
        x = np.arange(1, 101)
        ax.axhline(1, color=GRAY, linestyle="--", linewidth=1, label="WCC 控制：1")
        ax.plot(x, bare_time / control_time, linestyle="none", marker="s", markersize=4.2,
                markerfacecolor="white", markeredgewidth=0.8, color=ORANGE, label="裸操作级候选")
        ax.plot(x, retained_time / control_time, linestyle="none", marker="o", markersize=2.3,
                color=BLUE, label="保留较优的组合")
        ax.set_yscale("log", base=2)
        ax.set(xlim=(0, 101), ylim=(0.20, 2.65), xlabel="题图编号（全 100 图，未剔除慢图）",
               ylabel="时间比 T / T控制（log₂，越低越好）")
        ax.yaxis.set_major_locator(FixedLocator([0.25, 0.5, 1, 2]))
        ax.yaxis.set_major_formatter(FixedFormatter(["0.25", "0.5", "1", "2"]))
        ax.yaxis.set_minor_formatter(NullFormatter())
        ax.set_xticks([1, 20, 40, 60, 80, 100], ["001", "020", "040", "060", "080", "100"])
        ax.grid(axis="y", alpha=0.18, linewidth=0.5)
        ax.set_title("a  逐图同轴比较", loc="left", pad=12)
        handles, labels = ax.get_legend_handles_labels()
        fig.legend(handles, labels, loc="lower left", bbox_to_anchor=(0.085, 0.084),
                   frameon=False, ncol=3, columnspacing=1.5, fontsize=7.5)
        bars = aggregate.bar([0, 1, 2], means, color=[GRAY, ORANGE, BLUE], width=0.68,
                             edgecolor="#202020", linewidth=0.5)
        bars[1].set_hatch("//"); bars[2].set_hatch("..")
        aggregate.set(ylim=(0, 4.55), ylabel=r"官方均值  mean($B_i / T_i$)")
        aggregate.set_xticks([0, 1, 2], ["WCC\n控制", "裸操作\n候选", "保留\n组合"])
        aggregate.set_yticks([0, 1, 2, 3, 4])
        aggregate.grid(axis="y", alpha=0.18, linewidth=0.5)
        aggregate.set_axisbelow(True)
        aggregate.set_title("b  同一官方指标", loc="left", pad=12)
        for bar, value in zip(bars, means):
            aggregate.text(bar.get_x() + bar.get_width() / 2, value + 0.09, f"{value:.4f}", ha="center", fontsize=8.3)
        aggregate.text(1, 4.35, "裸候选略降；组合上升", ha="center", fontsize=7.2)
        fig.text(0.09, 0.945, "全 100 图：裸候选的退化不能被平均收益掩盖", fontsize=11)
        fig.text(0.09, 0.888, "裸操作级：44 胜 / 5 平 / 51 负     保留组合：44 胜 / 56 平 / 0 负", fontsize=8.7)
        fig.text(0.09, 0.045, "历史组合探索 · P2 / 5 核 · 全 100 图；不含后续 trace/cache，不是等预算冷启动比较。", fontsize=7.0)
        export(fig, "figure1_all100_portfolio", outputs)

        fig = plt.figure(figsize=(180 / 25.4, 184 / 25.4))
        ax = fig.add_axes([0.105, 0.46, 0.86, 0.365])
        simple_axis(ax)
        y = np.arange(len(mechanism))
        ax.axvline(0, color="#666666", linewidth=0.85, linestyle="--")
        for field, offset, marker, color, label, filled in (
                ("H_minus1_pct", -0.18, "o", BLUE, "H 固定计划硬件收益", True),
                ("S_minus1_pct", 0, "^", ORANGE, "S 缓存条件下策略收益", True),
                ("R_minus1_pct", 0.18, "s", GRAY, "R 总收益", False)):
            ax.scatter([r[field] for r in mechanism], y + offset, marker=marker, s=20,
                       facecolors=color if filled else "white", edgecolors=color, linewidths=0.9, label=label, zorder=3)
        ax.set(ylim=(9.6, -0.6), xlim=(-1.6, 19.1), xlabel="加速比相对 1 的变化（%，越大越好）")
        ax.set_yticks(y, [r["case"].split("_")[1] for r in mechanism])
        ax.set_xticks([0, 5, 10, 15])
        ax.grid(axis="x", alpha=0.18, linewidth=0.5)
        ax.set_title("a  10 图四格归因：H × S = R", loc="left", pad=12)
        handles, labels = ax.get_legend_handles_labels()
        fig.legend(handles, labels, loc="upper left", bbox_to_anchor=(0.1, 0.905), frameon=False, ncol=3,
                   columnspacing=1.05, handletextpad=0.35, fontsize=7.3)
        fig.text(0.105, 0.956, "P3 收益取决于计划：硬件、策略与重编码分别核算", fontsize=10.6)
        fig.text(0.105, 0.923, "预定机制集含开发图 · N=5 · 同一横轴；微小负策略收益保留数值，不放大柱长。", fontsize=7.5)
        ax.text(0.99, 0.98, "8 图采用同计划：S = 1\n不是已证明无策略空间", transform=ax.transAxes,
                ha="right", va="top", fontsize=7.6, color="#333333", linespacing=1.5)
        left = fig.add_axes([0.075, 0.11, 0.46, 0.235]); left.set_axis_off()
        right = fig.add_axes([0.585, 0.11, 0.385, 0.235]); right.set_axis_off()
        left.text(0, 1.1, "b  071：旧起点的两轮控制消融", fontsize=9.0, transform=left.transAxes)
        table(left, ["路径 / 数值类型", "起点", "第 1 轮", "第 2 轮"],
              [["强制纯重编码 / 候选", *ablation_values], ["严格保优 / incumbent", 8190, 8190, 8190],
               ["旧完整 cache / 保留解", *full_values]], bbox=[0, 0.35, 1, 0.68],
              widths=[0.46, 0.17, 0.185, 0.185], font_size=7.2)
        left.text(0, 0.21, "强制控制继续沿变差方案生成；严格保优仍为 8190。\n旧完整路径的最后胜者是重编码控制。", fontsize=7.0,
                  transform=left.transAxes, va="top", linespacing=1.6)
        right.text(0, 1.1, "c  冻结四格：周期数直接比较", fontsize=8.9, transform=right.transAxes)
        by_case = {r["case"]: r for r in mechanism}
        small = []
        for name in ("case_071", "case_093"):
            for plan in ("pi2", "pi3"):
                small.append([name[-3:] + ("  π₂" if plan == "pi2" else "  π₃"), by_case[name]["t2_" + plan], by_case[name]["t3_" + plan]])
        table(right, ["图 / 计划", "P2", "P3"], small, bbox=[0, 0.22, 1, 0.81], font_size=8.0)
        right.text(0, 0.12, "071：π₃ 比 π₂ 慢 7 周期。\n093：π₃ 比 π₂ 快 82 周期。", fontsize=7.2,
                   transform=right.transAxes, va="top", linespacing=1.6)
        fig.text(0.075, 0.054, "H = T₂(π₂)/T₃(π₂)；S = T₃(π₂)/T₃(π₃)；R = T₂(π₂)/T₃(π₃)。无置信区间或合成曲线。", fontsize=7.0)
        fig.text(0.075, 0.029, "071 旧路径 8190→7952 与当前 π₂ 的 7945 属于不同起点；补评后未回流修改冻结选择。", fontsize=7.0)
        export(fig, "figure2_p3_attribution", outputs)

    descriptions = {
        "figure1_all100_portfolio": {
            "alt": "全100图的时间比点图与官方均值柱图。裸操作级44图更快、5图持平、51图更慢；保留控制解的组合44图更快、56图持平。官方加速均值由控制3.3371变为裸候选3.2854、组合3.9462。",
            "caption": "历史 operation_all100_n5_v1 探索，P2、5核、全部100图，无剔除。左：固定题图顺序，纵轴T/Tcontrol为以2为底的对数轴，方形为裸操作级最优候选、圆点为保留控制的组合，控制参考线为1；同一位置的两种标记同时保留。右：官方逐图B_i/T_i算术均值，B_i来自整图singlecore入口，柱轴从0开始。不是mean(Tcontrol/T)，也不是平均时间之比。不含后续trace/cache；历史控制与新候选并非等预算冷启动比较。1094条候选记录全部成功。确定性有限图集描述统计，不绘CI。"},
        "figure2_p3_attribution": {
            "alt": "十个开发机制图的硬件、策略、总加速比在共同横轴上显示。八图没有针对当前计划的缓存策略搜索，S恒为1。071冻结策略比当前P2计划在P3下慢7周期，093快82周期。071旧起点强制纯重编码为8190、8234、8246；严格保优保持8190；旧完整搜索为8190、8189、7952。",
            "caption": "预定机制集011、019、044、049、051、064、069、071、082、093，含开发用例，N=5，不称held-out。H、S、R按冻结精确计划的四格定义，横轴为100×(比值−1)，不是时间缩短百分比，三个序列使用相同尺度；同一图中小的纵向偏移只区分标记。八图π3=π2，S=1不证明策略搜索无收益。071和093微小差直接列周期表，不用截轴柱夸大。071纯控制第一行是强制继续的候选值，第二行是严格保优incumbent（第二步由已评相同计划hash推得）；旧完整cache路径的末轮胜者仍是重编码控制。旧起点与当前四格起点不同，不能混用。无重复实验CI或平滑拟合。"}}
    (OUT / "captions_and_alt_text.json").write_text(json.dumps(descriptions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    provenance = {"created_utc": datetime.now(timezone.utc).isoformat(), "inputs": before,
                  "inputs_unchanged_after_render": all(sha(SOURCES[k]) == v["sha256"] for k, v in before.items()),
                  "script_path": str(Path(__file__).resolve()), "script_sha256": sha(__file__),
                  "font_path": str(FONT), "font_sha256": sha(FONT), "font_family": font_name,
                  "python": sys.version, "python_executable": sys.executable, "platform": platform.platform(),
                  "matplotlib": matplotlib.__version__, "numpy": np.__version__,
                  "destination": "general manuscript figures, provisional dimensions; no journal-specific compliance claimed",
                  "figures": outputs,
                  "transformations": ["all100: fixed case order, T/control normalization, log2 y-axis", "official mean = mean(singlecore_i/time_i)",
                                      "P3: exact per-case H/S/R ratios; display 100*(ratio-1) on common linear x-axis", "table entries are actual recorded cycles"],
                  "no_exclusions": True, "uncertainty": "none: finite deterministic case descriptions, not replicated sampling",
                  "no_smoothing_or_fitted_curves": True, "no_synthetic_performance_data": True,
                  "skill": {"path": "/Users/liyu/.codex/skills/scientific-visualization/SKILL.md",
                            "reference_verified_2026_09_24": "Kassis, T., Agarwal, V., He, Y., Patel, D., & Brueckner, A. M. (2026). Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents. https://doi.org/10.48550/arXiv.2609.00065"},
                  "data_files": {p.name: sha(p) for p in sorted(OUT.glob("*.csv"))}}
    (OUT / "manifest.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"figures": [o["name"] for o in outputs], "source_hashes_unchanged": provenance["inputs_unchanged_after_render"],
                      "official_means": means, "counts": [wins, ties, losses]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
