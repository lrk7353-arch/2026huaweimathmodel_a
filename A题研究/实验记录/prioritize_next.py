"""Rank next investigations using loose lower bounds and measured best plans.

Bounds omit synchronization, contention timing and memory overhead; headroom is
only an upper bound on possible improvement, not a predicted attainable gain.
"""
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def main():
    structure = ROOT / "方案审阅/结构核验/a_graph_structure.csv"
    baseline = ROOT / "solver/runs/full_initial_v1/reports/per_case.csv"
    portfolio = ROOT / "当前最佳方案/catalog.csv"
    geometry = {r["case"]: r for r in read(structure)}
    measured = {r["case"]: r for r in read(baseline)
                if r["method"] == "simple" and r["problem"] == "2" and r["num_cores"] == "5"}
    known = {r["case"]: r for r in read(portfolio) if r["problem"] == "2" and r["num_cores"] == "5"}
    if len(measured) != 100 or len(known) != 100:
        raise ValueError("Require complete all-100 P2/N5 measured and portfolio rows")
    rows = []
    for case in sorted(measured):
        s, b, k = geometry[case], measured[case], known[case]
        # Do not use the whole-component lower bound: internal splitting is allowed.
        terms = {name: float(s["five_core_baseline_lb_" + name]) for name in ("M", "V", "DDR", "CP")}
        lower = max(terms.values())
        elapsed = int(k["makespan"])
        if lower <= 0 or elapsed < lower - 1e-6:
            raise ValueError("Check lower bound vs official time for " + case)
        rows.append({"case": case, "best_known_p2_n5": elapsed,
                     "baseline_speedup": b["main_speedup"], "loose_lower_bound": lower,
                     "largest_bound_term_not_bottleneck": max(terms, key=terms.get),
                     "time_to_loose_bound_ratio": elapsed / lower,
                     "possible_relative_reduction_upper_bound": 1 - lower / elapsed,
                     "largest_component_fraction": s["largest_component_fraction"],
                     "compute_ops": s["compute_ops"], "dependency_components": s["dependency_components"],
                     "current_best_origin": k["origin"]})
    rows.sort(key=lambda r: (-r["time_to_loose_bound_ratio"], r["case"]))
    out = ROOT / "实验记录/下一轮优先图"
    out.mkdir(parents=True, exist_ok=True)
    with (out / "all_100.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    lines = ["# 下一轮优先排查图（P2、5核）", "",
             "依据已保存最好时间与计算/DDR/纯计算路径的宽松下界排序。下界忽略同步、时序争用和内存开销，因此所谓余量只是上界，不保证可达到；最大下界项也不等于真实瓶颈。未使用整分量下界，因为下一轮允许拆分分量。", "",
             "下表只用于决定先诊断哪些图；正式算法规则不按这些题号写特殊答案。完整100图在相邻CSV。", "",
             "|图|当前最好周期|宽松下界|时间/下界|最大分量节点占比|", "|---|---:|---:|---:|---:|"]
    for r in rows[:15]:
        lines.append(f"|{r['case']}|{r['best_known_p2_n5']:,}|{r['loose_lower_bound']:.1f}|{r['time_to_loose_bound_ratio']:.2f}|{float(r['largest_component_fraction']):.1%}|")
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    sources = [structure, baseline, portfolio, Path(__file__)]
    (out / "provenance.json").write_text(json.dumps({"source_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
                                                    "scope": "100 measured P2/N5 cases; diagnosis priority, not performance prediction"}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(out / "README.md")


if __name__ == "__main__":
    main()
