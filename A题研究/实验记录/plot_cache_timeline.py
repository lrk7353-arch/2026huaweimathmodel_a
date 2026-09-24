"""Plot measured operation intervals for one independently verified fixed plan."""
import csv
import gzip
import json
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from plot_first_round import export, sha

HERE = Path(__file__).resolve().parent


def main():
    verification = HERE / "independent_heft_check/verification.json"
    evidence = json.loads(verification.read_text())
    selected = next(r for r in evidence["cases"] if r["case"] == "case_071")
    results, sources = {}, [verification]
    for problem in (2, 3):
        record = selected["evaluations"][str(problem)]
        path = Path(record["result_path"])
        if record["status"] != "success" or sha(path) != record["result_sha256"]:
            raise ValueError("Independent official evidence missing or changed")
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            results[problem] = json.load(stream)
        sources.append(path)
    if selected["evaluations"]["2"]["hashes"]["plan_sha256"] != selected["evaluations"]["3"]["hashes"]["plan_sha256"]:
        raise ValueError("Paired plots require exactly the same plan")
    colors = {"M计算": "#0072B2", "V计算": "#B34D00", "读取": "#666666",
              "Cache命中读取": "#00755E", "写出": "#8C6BB1"}
    pipes = {"PIPE_M": 0, "PIPE_V": 1, "PIPE_MTE2": 2, "PIPE_MTE3": 3}
    rows = []
    with plt.rc_context({"font.family": "Arial Unicode MS", "font.size": 9,
                         "axes.unicode_minus": False, "pdf.fonttype": 42}):
        fig, axes = plt.subplots(2, 1, figsize=(12, 9.5), sharex=True, layout="constrained")
        for ax, problem in zip(axes, (2, 3)):
            result = results[problem]
            for core in result["per_core_timeline"]:
                core_id = core["core_id"]
                groups = defaultdict(list)
                ends = []
                for op in core["ops"]:
                    start, end = op["start"], op["end"]
                    if start < 0 or end < start or end - start != op["duration"]:
                        raise ValueError("Invalid measured operation interval")
                    pipe = op["pipe"]
                    if pipe not in pipes:
                        raise ValueError("Unknown pipe")
                    label = {"PIPE_M": "M计算", "PIPE_V": "V计算", "PIPE_MTE2": "读取", "PIPE_MTE3": "写出"}[pipe]
                    if op.get("cache_hit"):
                        label = "Cache命中读取"
                    groups[pipe, label].append((start, end - start))
                    ends.append(end)
                    rows.append({"problem": problem, "core": core_id, "pipe": pipe,
                                 "op_id": op["op_id"], "start": start, "end": end,
                                 "duration": end - start, "cache_hit": bool(op.get("cache_hit", False))})
                if core_id % 2 == 0:
                    ax.axhspan(core_id * 5 - .2, core_id * 5 + 4.2, color="#F3F3F3", zorder=0)
                for (pipe, label), intervals in groups.items():
                    ax.broken_barh(intervals, (core_id * 5 + pipes[pipe], .72),
                                   facecolors=colors[label], edgecolors="none")
                ax.text(10350, core_id * 5 + 1.8, f"{max(ends):,}", va="center", fontsize=8)
            ax.axvline(result["makespan"], linestyle="--", color="#222222", linewidth=.9)
            ax.set(yticks=[k * 5 + 1.8 for k in range(5)], yticklabels=[f"核 {k}" for k in range(5)],
                   ylim=(24.6, -.8), xlim=(0, 10800), ylabel="每核四行：M、V、读取、写出（从上到下）",
                   title=f"P{problem}  {'无共享只读Cache' if problem == 2 else '加入共享只读Cache'}：Makespan = {result['makespan']:,} 周期")
            ax.spines[["top", "right"]].set_visible(False)
            ax.grid(axis="x", color="#DDDDDD", linewidth=.5)
            ax.set_axisbelow(True)
        axes[-1].set_xlabel("官方模拟时间（周期；两面板使用相同尺度）")
        axes[0].legend(handles=[Patch(facecolor=c, label=k) for k, c in colors.items()],
                       ncols=5, fontsize=8, loc="upper center", bbox_to_anchor=(.5, 1.2))
        fig.suptitle("用例071：固定五核方案的实际执行时间线\n右侧数字为各核完成时间；绿色表示命中Cache的读取", fontsize=12)
        out = HERE / "figures/071固定方案缓存时间线"
        export(fig, out, sources + [Path(__file__)],
               "上下两图分别是同一个五核方案在P2与P3的原版官方操作时间线。每核显示M、V、读取、写出四条流水线。P3的Cache命中读取为绿色，各核完成时间均降低，最后完成的核2从10051降至8212周期。两图使用相同时间轴；不把这个对照当作单个命中事件的因果归因。",
               ["Require identical plan SHA across P2/P3 and verify both official gzip hashes.",
                "Draw every measured operation interval without time interpolation.",
                "Lane categories are compute/read/write; read includes all MTE2 traffic, green is explicitly cache_hit=true.",
                "Per-core finish=max end over all reported operations; same time scale in both panels."])
        with out.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader(); writer.writerows(rows)
        print(out.with_suffix(".png"))


if __name__ == "__main__":
    main()
