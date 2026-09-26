"""Re-express the released ledgers using the problem statement's definitions."""
import csv
import hashlib
import json
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parent
RELEASE = ROOT/"真机启发攻坚_20260926"
OUT = ROOT/"统一算法再攻坚研究_20260926/题目口径核验"


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, data):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(data[0]))
        w.writeheader()
        w.writerows(data)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    curves, pairs, summary, sources = [], [], [], {}
    for label, path in [
        ("cold", RELEASE/"从头完整1500/从头1500配置成绩.csv"),
        ("selected", RELEASE/"精选完整1500/累计1500配置成绩.csv"),
    ]:
        data = rows(path)
        index = {(r["case"], int(r["problem"]), int(r["cores"])): r for r in data}
        assert len(index) == len(data) == 1500
        assert set(index) == {(f"case_{i:03d}", p, n) for i in range(1,101)
                             for p in range(1,4) for n in range(1,6)}
        sources[label] = dict(path=str(path.relative_to(ROOT)),
                             sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        for p in range(1,4):
            for n in range(1,6):
                group = [index[f"case_{i:03d}",p,n] for i in range(1,101)]
                curves.append(dict(library=label, problem=p, cores=n,
                    statement_mean_speedup=1. if n==1 else mean(float(r["speedup"]) for r in group),
                    optimized_onecore_reference_ratio=mean(float(r["speedup"]) for r in group) if n==1 else "",
                    count=100))
        for n in range(1,6):
            group = []
            for i in range(1,101):
                a,b = index[f"case_{i:03d}",2,n],index[f"case_{i:03d}",3,n]
                row=dict(library=label, case=f"case_{i:03d}", cores=n,
                    no_l2_makespan=int(a["makespan"]), l2_makespan=int(b["makespan"]),
                    no_l2_added_copy=int(a["added_copy"]), l2_added_copy=int(b["added_copy"]),
                    same_core_p2_over_p3=int(a["makespan"])/int(b["makespan"]),
                    same_plan_hash=a["plan_sha256"]==b["plan_sha256"],
                    interpretation="separately optimized P2/P3 plans; includes scheduling differences")
                pairs.append(row);group.append(row)
            summary.append(dict(library=label,cores=n,count=100,
                mean_same_core_p2_over_p3=mean(r["same_core_p2_over_p3"] for r in group),
                l2_faster=sum(r["same_core_p2_over_p3"]>1 for r in group),
                equal=sum(r["same_core_p2_over_p3"]==1 for r in group),
                l2_slower=sum(r["same_core_p2_over_p3"]<1 for r in group),
                identical_plan_hashes=sum(r["same_plan_hash"] for r in group)))
    write_csv(OUT/"按题意单核为1的曲线.csv",curves)
    write_csv(OUT/"P3同核数逐图对照.csv",pairs)
    write_csv(OUT/"P3同核数汇总.csv",summary)
    (OUT/"sources.json").write_text(json.dumps(sources,ensure_ascii=False,indent=2)+"\n")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1,2,figsize=(11,4.2))
    for p,color in [(1,"#2878b5"),(2,"#e07b39"),(3,"#38905c")]:
        for label,style in [("cold","-"),("selected","--")]:
            g=[r for r in curves if r["problem"]==p and r["library"]==label]
            axes[0].plot([r["cores"] for r in g],[r["statement_mean_speedup"] for r in g],
                         style,marker="o",color=color,label=f"P{p} {label}")
    axes[0].set(xlabel="Cores",ylabel="Mean speedup vs original single-core",
                title="Submission curve: single-core point = 1",xticks=range(1,6))
    for label,style in [("cold","-"),("selected","--")]:
        g=[r for r in summary if r["library"]==label]
        axes[1].plot([r["cores"] for r in g],[r["mean_same_core_p2_over_p3"] for r in g],
                     style,marker="o",label=label)
    axes[1].axhline(1,color="gray",linewidth=.8)
    axes[1].set(xlabel="Cores",ylabel="Mean of per-graph T(P2) / T(P3)",
                title="Separate P2/P3 optimized plans",xticks=range(1,6))
    for ax in axes:
        ax.legend(fontsize=8);ax.grid(alpha=.2)
    fig.tight_layout()
    fig.savefig(OUT/"题目口径曲线.png",dpi=180)
    fig.savefig(OUT/"题目口径曲线.pdf")
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
