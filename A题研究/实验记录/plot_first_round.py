"""Truthful first-round research figures from saved CSVs, not predicted scores.

Run with a Matplotlib environment, e.g. the existing E题研究/.venv/bin/python.
The solver itself remains stdlib-only. No interpolation at unmeasured core counts.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, MaxNLocator
from PIL import Image


def read_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def export(fig, path, inputs, alt, transforms):
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path.with_suffix(".png"), dpi=200, facecolor="white")
    fig.savefig(path.with_suffix(".pdf"), facecolor="white")
    with Image.open(path.with_suffix(".png")) as im:
        metadata = {"pixels": list(im.size), "mode": im.mode, "dpi": im.info.get("dpi"),
                    "alpha_extrema": im.getchannel("A").getextrema() if im.mode == "RGBA" else None}
    record = {"inputs_sha256": {str(p.resolve()): sha(p) for p in inputs},
              "script_sha256": sha(__file__), "matplotlib": matplotlib.__version__,
              "purpose": "internal modeling research report; contest formatting pending final verification",
              "transforms": transforms, "uncertainty": "none; one deterministic saved run per case, no seed uncertainty claimed",
              "missing_policy": "required observations must all be present or generation fails",
              "alt_text": alt, "metadata": metadata,
              "physical_size_inches": list(fig.get_size_inches()),
              "method_reference": "Kassis T., Agarwal V., He Y., Patel D., Brueckner A.M. (2026). Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents. https://doi.org/10.48550/arXiv.2609.00065"}
    path.with_suffix(".provenance.json").write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    plt.close(fig)


def make_pilot(run, out):
    path = run / "reports" / "per_case.csv"
    pairs_path = run / "reports" / "cache_pairs.csv"
    rows, pairs = read_rows(path), read_rows(pairs_path)
    cases = sorted({r["case"] for r in rows})
    lookup = {(r["case"],r["method"],r["problem"],r["num_cores"]):r for r in rows}
    pair_lookup = {(r["case"],r["method"],r["num_cores"]):r for r in pairs}
    colors, markers = ("#0072B2", "#B34D00"), ("o", "s")
    fig, axes = plt.subplots(1,2,figsize=(12,4.9),layout="constrained")
    x = list(range(len(cases)))
    for idx, method in enumerate(("simple","affinity")):
        values = [float(lookup[(c,method,"2","5")]["main_speedup"]) for c in cases]
        axes[0].bar([i+(-.18 if idx==0 else .18) for i in x],values,width=.34,
                    color=colors[idx],edgecolor="#222222",linewidth=.45,
                    hatch="" if idx==0 else "//",label="简单装箱" if idx==0 else "共享输入候选组合")
    axes[0].axhline(1,color="#555555",linestyle=":",linewidth=1)
    axes[0].set(xticks=x,xticklabels=[c[-3:] for c in cases],xlabel="正式用例编号",
                ylabel="加速比（整图单核 / 五核时间）",ylim=(0,5.5),title="A  五核P2：整分量方法的能力边界")
    axes[0].legend(loc="upper right",fontsize=8)
    vals = [float(pair_lookup[(c,"affinity","5")]["total"]) for c in cases]
    axes[1].bar(x,vals,color="#0072B2",edgecolor="#222222",linewidth=.45)
    axes[1].axhline(1,color="#222222",linestyle="--",linewidth=1,label="无改善：1倍")
    for i,v in enumerate(vals):
        axes[1].text(i,v+.02,f"{v:.3f}",ha="center",fontsize=8,rotation=45)
    axes[1].set(xticks=x,xticklabels=[c[-3:] for c in cases],xlabel="正式用例编号",
                ylabel="同核数时间比 T2(π2) / T3(π3)",ylim=(0,1.35),title="B  五核P3：现有候选池的缓存总收益")
    axes[1].legend(loc="upper right",fontsize=8)
    for ax in axes:
        ax.spines[["top","right"]].set_visible(False)
        ax.grid(axis="y",color="#DDDDDD",linewidth=.5)
        ax.set_axisbelow(True)
    fig.suptitle(f"首轮代表图：{len(cases)}图，全候选评估、单种子；尚无大分量内部切分",fontsize=12)
    export(fig,out/"代表图基线与缓存收益",[path,pairs_path],
           "两个并列柱状图：左图比较9个用例的简单装箱和共享输入候选组合的五核P2加速比；右图显示五核P3相对P2的总时间比，接近1说明当前收益很小。所有柱从零起。",
           ["Select P2, configured cores=5, two baseline methods; retain every declared case.",
            "P2 speedup=official whole-graph singlecore makespan / actual P2 makespan.",
            "P3 total ratio=T2(pi2)/T3(pi3); bar baseline is zero."])


def make_full(run, structure, out):
    path = run / "reports" / "per_case.csv"
    rows, structure_rows = read_rows(path), read_rows(structure)
    lookup = {(r["case"],r["problem"],r["num_cores"]):r for r in rows if r["method"]=="simple"}
    cases = sorted({r["case"] for r in rows})
    if len(cases) != 100:
        raise ValueError("Full graph requires all 100 cases")
    structural = {r["case"]:r for r in structure_rows}
    fig, axes = plt.subplots(1,2,figsize=(12,4.9),layout="constrained")
    for cores,color,marker in ((2,"#0072B2","o"),(5,"#B34D00","^")):
        values = [float(lookup[(c,"2",str(cores))]["main_speedup"]) for c in cases]
        x = [float(structural[c]["largest_component_fraction"])*100 for c in cases]
        axes[0].scatter(x,values,s=22,marker=marker,color=color,alpha=.7,
                        label=f"{cores}核（100图）",edgecolor="white",linewidth=.3)
        # Empirical CDF uses every saved case. Steps show observations without smoothing.
        ordered = sorted(values)
        axes[1].step(ordered,[(i+1)/100 for i in range(100)],where="post",color=color,
                     linestyle="-" if cores==2 else "--",label=f"{cores}核：均值{statistics.mean(values):.3f}倍")
    axes[0].axhline(1,color="#555555",linestyle=":",linewidth=1)
    axes[0].set(xlabel="最大计算依赖分量的节点占比（%）",ylabel="P2加速比（单核 / 多核）",
                title="A  大分量占主导时，整分量装箱受限",xlim=(-2,102),ylim=(0,None))
    axes[1].set(xlabel="P2加速比（单核 / 多核）",ylabel="用例累计比例",title="B  全100图的实际加速比分布",
                xlim=(0,None),ylim=(0,1.03))
    axes[1].axvline(1,color="#555555",linestyle=":",linewidth=1)
    for ax in axes:
        ax.spines[["top","right"]].set_visible(False)
        ax.grid(color="#DDDDDD",linewidth=.5)
        ax.set_axisbelow(True)
        ax.legend(fontsize=8,loc="best")
    fig.suptitle("100图首轮基线：整分量装箱，每配置最多4候选；当前覆盖2核与5核",fontsize=12)
    export(fig,out/"全100图结构与基线表现",[path,structure],
           "左图以最大依赖分量占比为横轴、P2加速比为纵轴，显示2核和5核的全部100个用例；右图为两组加速比的经验累积分布。单大分量使整分量方法只能用一个核。未展示尚未测试的3核或4核结果。",
           ["Select simple P2, configured cores=2 or 5; require all 100 cases.",
            "Largest component fraction is from independent structure audit, not runtime-derived bottleneck proof.",
            "Empirical CDF uses sorted individual speedups, no binning or smoothing."])


def make_difficult(verification, out):
    data=json.loads(verification.read_text(encoding="utf-8"))
    if not data["all_eight_fresh_success"] or not data["all_p2_match"]:
        raise ValueError("Independent verification must pass before plotting")
    fig,axes=plt.subplots(2,2,figsize=(11,7.1),layout="constrained")
    labels=["整分量装箱（P2）","连续拓扑块（P2）","操作级分核（P2）","同方案加L2（P3）"]
    colors=["#707070","#0072B2","#B34D00","#00755E"]
    for ax,row in zip(axes.flat,data["cases"]):
        values=[row["wcc_baseline"],row["partition_baseline"],
                row["evaluations"]["2"]["metrics"]["makespan"],row["evaluations"]["3"]["metrics"]["makespan"]]
        bars=ax.barh(range(4),values,color=colors,edgecolor="#222222",linewidth=.5)
        for bar,hatch in zip(bars,("","//","..","xx")):bar.set_hatch(hatch)
        for i,value in enumerate(values):
            ax.text(value+max(values)*.018,i,f"{value:,}",va="center",fontsize=9)
        ax.set(yticks=range(4),yticklabels=labels,xlim=(0,max(values)*1.27),
               xlabel="Makespan（千周期；越小越好）",title=f"用例{row['case'][-3:]}：操作级方案实际使用5核")
        ax.invert_yaxis()
        ax.spines[["top","right"]].set_visible(False)
        ax.xaxis.set_major_locator(MaxNLocator(5))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda x,pos:f"{x/1000:g}"))
        ax.grid(axis="x",color="#DDDDDD",linewidth=.5);ax.set_axisbelow(True)
    fig.suptitle("四张困难图的改进路径：已独立复现；柱尾为周期数，各面板横轴范围不同",fontsize=12)
    export(fig,out/"困难图改进路径",[verification],
           "四个面板分别显示用例051、071、064、049的整分量装箱、连续拓扑块、操作级分核P2及同一操作级方案P3的实际周期数。所有横轴从零开始且每根柱直接标数；每个面板尺度不同，不能跨面板比较柱长。结果仅覆盖四张预选困难图。",
           ["Read four independent fresh P2 replays and exact-plan P3 measurements.",
            "Previous WCC and partition controls copied from hashed verification record.",
            "Absolute cycles, no normalization; x-axis shown in thousands of cycles and bar-end labels in cycles; separate clearly labeled panel scales."])


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--pilot",type=Path)
    p.add_argument("--full",type=Path)
    p.add_argument("--difficult",type=Path)
    p.add_argument("--structure",type=Path,default=Path(__file__).resolve().parents[1]/"方案审阅/结构核验/a_graph_structure.csv")
    p.add_argument("--out",type=Path,default=Path(__file__).resolve().parent/"figures")
    a=p.parse_args()
    with plt.rc_context({"font.family":"Arial Unicode MS","font.size":9,"axes.unicode_minus":False,
                         "pdf.fonttype":42,"ps.fonttype":42,"savefig.transparent":False}):
        if a.pilot:make_pilot(a.pilot,a.out)
        if a.full:make_full(a.full,a.structure,a.out)
        if a.difficult:make_difficult(a.difficult,a.out)


if __name__=="__main__":main()
