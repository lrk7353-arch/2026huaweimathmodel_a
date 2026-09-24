"""Build reproducible CSV/Markdown reports from saved official evaluations."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from pathlib import Path
import statistics
import sys

sys.dont_write_bytecode = True
from common import atomic_json, read_json


def write_csv(path, rows, fields):
    with Path(path).open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def summarize(run_dir):
    run_dir = Path(run_dir).resolve()
    manifest = read_json(run_dir / "manifest.json")
    spec = manifest["specification"]
    expected = len(spec["inputs"])
    rows, pairs, baseline_rows, attempts = [], [], [], []
    missing = []
    pair_statuses = []
    for filename in spec["inputs"]:
        case = Path(filename).stem
        directory = run_dir / "results" / case
        basefile = directory / "singlecore.json"
        baseline = read_json(basefile) if basefile.exists() else {"status": "missing"}
        b = baseline.get("metrics", {}).get("makespan") if baseline["status"] == "success" else None
        baseline_rows.append({"case": case, "status": baseline["status"], "makespan": b,
                              "elapsed_seconds": baseline.get("elapsed_seconds"),
                              "result_path": baseline.get("result_path")})
        for method in spec["methods"]:
            for problem in spec["problems"]:
                for cores in spec["cores"]:
                    resultfile = directory / f"{method}_p{problem}_n{cores}_seed{spec['seed']}.json"
                    row = {"case": case, "method": method, "problem": problem, "num_cores": cores,
                           "baseline_makespan": b, "status": "missing"}
                    if not resultfile.exists():
                        missing.append(str(resultfile))
                        rows.append(row)
                        continue
                    result = read_json(resultfile)
                    row.update({k: result[k] for k in ("status", "elapsed_seconds", "generation_seconds",
                                                      "candidate_count", "evaluated_count", "official_calls",
                                                      "cache_hits", "stop_reason")})
                    for attempt in result["evaluations"]:
                        record = attempt["record"]
                        attempts.append({"case": case, "method": method, "problem": problem,
                                         "num_cores": cores, "candidate": attempt["candidate"],
                                         "status": record["status"], "elapsed_seconds": record.get("elapsed_seconds"),
                                         "cache_hit": record.get("cache_hit"),
                                         "error": record.get("error"),
                                         "peak_memory_bytes": record.get("metrics", {}).get("peak_memory_bytes"),
                                         "result_path": record.get("result_path")})
                    if result["best"]:
                        best = result["best"]
                        metrics = best["record"]["metrics"]
                        movement = metrics.get("data_movement_bytes", {})
                        cache = metrics.get("cache_stats", {})
                        row.update({"candidate": best["candidate"], "makespan": metrics["makespan"],
                                    "active_cores": sum(bool(c) for c in best["plan"]["core_schedules"]),
                                    "subgraphs": len(set(best["plan"]["node_to_subgraph"].values())),
                                    "added_copy_bytes": movement.get("added_copy_bytes"),
                                    "scheduled_copy_bytes": movement.get("scheduled_copy_bytes"),
                                    "spill_added_copy_bytes": movement.get("spill_added_copy_bytes"),
                                    "cache_hit_rate_bytes": cache.get("hit_rate"),
                                    "result_path": best["record"].get("result_path"),
                                    "solve_path": str(resultfile)})
                        if b is not None and metrics["makespan"] > 0:
                            row["actual_singlecore_ratio"] = b / metrics["makespan"]
                            if problem in (1,2):
                                row["main_speedup"] = 1.0 if cores == 1 else b / metrics["makespan"]
                    rows.append(row)
            for cores in spec["cores"] if 2 in spec["problems"] and 3 in spec["problems"] else []:
                pairfile = directory / f"{method}_cache_pair_n{cores}_seed{spec['seed']}.json"
                if pairfile.exists():
                    pair = read_json(pairfile)
                    pair_statuses.append({"case": case, "method": method, "num_cores": cores,
                                          "status": "success" if len(pair["ratios"]) == 3 else "incomplete",
                                          "cell_statuses": {k: v["status"] for k,v in pair["cells"].items()}})
                    pairs.append({"case": case, "method": method, "num_cores": cores,
                                  **pair["ratios"], **{k: v.get("metrics", {}).get("makespan")
                                                      for k,v in pair["cells"].items()},
                                  "pair_path": str(pairfile)})
                else:
                    missing.append(str(pairfile))
                    pair_statuses.append({"case": case, "method": method, "num_cores": cores,
                                          "status": "missing", "cell_statuses": {}})
    aggregates = []
    groups = defaultdict(list)
    for row in rows:
        groups[(row["method"], row["problem"], row["num_cores"])].append(row)
    for (method, problem, cores), group in sorted(groups.items()):
        valid = [r for r in group if r.get("makespan") is not None and r.get("baseline_makespan") is not None]
        complete = len(valid) == expected
        item = {"method": method, "problem": problem, "num_cores": cores,
                "expected_cases": expected, "valid_cases": len(valid), "complete": complete,
                "mean_speedup": None, "mean_actual_ratio": None,
                "note": "complete declared subset" if complete else "incomplete; mean withheld"}
        if complete:
            item["mean_actual_ratio"] = statistics.mean(r["actual_singlecore_ratio"] for r in valid)
            if problem in (1,2):
                item["mean_speedup"] = statistics.mean(r["main_speedup"] for r in valid)
            item["mean_solve_seconds"] = statistics.mean(r["elapsed_seconds"] for r in valid)
            item["mean_evaluations"] = statistics.mean(r["evaluated_count"] for r in valid)
        aggregates.append(item)
    comparisons = []
    bykey = {(r["case"],r["problem"],r["num_cores"],r["method"]):r for r in rows}
    for problem in spec["problems"]:
        for cores in spec["cores"]:
            compared, wins, ties, losses = [], 0, 0, 0
            for filename in spec["inputs"]:
                key = (Path(filename).stem,problem,cores)
                simple, affinity = bykey.get(key+("simple",)), bykey.get(key+("affinity",))
                if not simple or not affinity or not simple.get("makespan") or not affinity.get("makespan"):
                    continue
                gain = simple["makespan"] / affinity["makespan"]
                compared.append(gain)
                wins += gain > 1
                ties += gain == 1
                losses += gain < 1
            if compared:
                comparisons.append({"problem": problem, "num_cores": cores, "paired_cases": len(compared),
                                    "wins": wins, "ties": ties, "losses": losses,
                                    "mean_simple_over_affinity": statistics.mean(compared),
                                    "scope": "candidate-portfolio comparison; inspect evaluation and wallclock budgets"})
    summary = {"created_utc": datetime.now(timezone.utc).isoformat(), "run_dir": str(run_dir),
               "manifest_signature": manifest["signature"], "scope": manifest["scope"],
               "expected_cases": expected, "expected_slots": len(rows),
               "successful_slots": sum(r["status"] == "success" for r in rows),
               "baseline_successes": sum(r["status"] == "success" for r in baseline_rows),
               "missing_files": missing, "aggregates": aggregates, "comparisons": comparisons,
               "pair_statuses": pair_statuses,
               "complete": (all(r["status"] == "success" for r in rows + baseline_rows + pair_statuses)),
               "candidate_status_counts": dict(Counter(a["status"] for a in attempts)),
               "limitations": ["No within-component splitting or bottleneck local search yet.",
                               "P3 uses the baseline candidate pool, without cache-specific reorder search.",
                               "Single seed; bounded portfolio results are not same-wallclock research comparisons.",
                               "Official makespans measure NPU cycles; solver walltime is a different metric."]}
    out = run_dir / "reports"
    out.mkdir(exist_ok=True)
    atomic_json(out / "summary.json", summary)
    write_csv(out / "per_case.csv", rows,
              ["case","method","problem","num_cores","status","baseline_makespan","makespan",
               "main_speedup","actual_singlecore_ratio","active_cores","subgraphs","candidate",
               "added_copy_bytes","scheduled_copy_bytes","spill_added_copy_bytes","cache_hit_rate_bytes",
               "elapsed_seconds","generation_seconds","candidate_count","evaluated_count","official_calls",
               "cache_hits","stop_reason","result_path","solve_path"])
    write_csv(out / "singlecore.csv", baseline_rows,
              ["case","status","makespan","elapsed_seconds","result_path"])
    write_csv(out / "candidate_attempts.csv", attempts,
              ["case","method","problem","num_cores","candidate","status","elapsed_seconds","cache_hit",
               "peak_memory_bytes","error","result_path"])
    write_csv(out / "cache_pairs.csv", pairs,
              ["case","method","num_cores","t2_pi2","t3_pi2","t2_pi3","t3_pi3","hardware","selection","total","pair_path"])
    lines = ["# A题首轮基线实验报告", "", f"结果目录：`{run_dir}`", "",
             f"覆盖：{expected}个声明用例，{summary['successful_slots']}/{len(rows)}个求解结果槽成功，"
             f"官方整图单核基准{summary['baseline_successes']}/{expected}。", "",
             "当前仅实现整分量装箱与共享输入感知候选；尚未实现大分量内部切分、瓶颈搜索及P3专项重排。"
             "结果使用原版官方评估器。候选组合和单随机种子结果用于建立基线，不能视为同墙钟预算下的最终算法优越性证据。", "",
             "## P1/P2平均逐图加速比", "",
             "P1/P2以官方整图单核时间为分子、多核时间为分母；k=1主图点按题面为1。采用逐图比值的算术平均。"
             "不完整分组不报平均值；少于100图的结果仅代表所选用例。", "",
             "求解耗时是本次调用的实际秒数，含成功结果缓存复用；简单与共享方法先后运行，不能据此比较冷启动求解效率。", "",
             "| 方法 | 场景 | 核数 | 覆盖 | 平均加速比 | 本次平均秒（含缓存） |", "|---|---|---:|---:|---:|---:|"]
    for item in aggregates:
        if item["problem"] not in (1,2):
            continue
        speed = f"{item['mean_speedup']:.4f}" if item["mean_speedup"] is not None else "未齐全"
        seconds = f"{item['mean_solve_seconds']:.3f}" if item.get("mean_solve_seconds") is not None else "—"
        lines.append(f"| {item['method']} | P{item['problem']} | {item['num_cores']} | {item['valid_cases']}/{expected} | {speed} | {seconds} |")
    lines += ["", "## 共享输入候选组合与简单装箱的比较", "",
              "胜/平/负以实际makespan比较；候选数量和耗时见逐图表，本表不宣称等计算预算。", "",
              "| 场景 | 核数 | 配对图数 | 胜 | 平 | 负 | 简单/共享方案时间比均值 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for item in comparisons:
        lines.append(f"| P{item['problem']} | {item['num_cores']} | {item['paired_cases']} | {item['wins']} | {item['ties']} | {item['losses']} | {item['mean_simple_over_affinity']:.4f} |")
    lines += ["", "## P3配对对照", "",
              "每例四格分别保存T2(π2)、T3(π2)、T2(π3)、T3(π3)。hardware衡量固定方案的缓存硬件收益，"
              "selection衡量同一候选池在P3下择优的收益；当前尚无缓存专项搜索。两个收益的乘积只逐例成立，均值不可相乘。", "",
              "| 方法 | 核数 | 四格完整图数 | 硬件收益均值 | 候选选择收益均值 | 总收益均值 |", "|---|---:|---:|---:|---:|---:|"]
    pgroups = defaultdict(list)
    if 2 in spec["problems"] and 3 in spec["problems"]:
        for method in spec["methods"]:
            for cores in spec["cores"]:
                pgroups[(method, cores)] = []
    for pair in pairs:
        if all(k in pair for k in ("hardware","selection","total")):
            pgroups[(pair["method"],pair["num_cores"])].append(pair)
    for (method,cores), group in sorted(pgroups.items()):
        ratios = [f"{statistics.mean(r[k] for r in group):.4f}" if len(group)==expected else "未齐全"
                  for k in ("hardware","selection","total")]
        lines.append(f"| {method} | {cores} | {len(group)}/{expected} | {' | '.join(ratios)} |")
    lines += ["", "## 明细与失败记录", "",
              f"候选状态：`{summary['candidate_status_counts']}`。超时不等于方案非法。", "",
              "- [逐图结果](per_case.csv)", "- [单核基准](singlecore.csv)",
              "- [全部候选评估状态](candidate_attempts.csv)", "- [P3四格对照](cache_pairs.csv)",
              "- [机器可读汇总](summary.json)", "",
              "静态scheduled/added COPY字节与P3实际DDR未命中字节分开解释，不能用命中率直接修改官方搬运字段。", ""]
    (out / "首轮基线实验报告.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", type=Path)
    args = p.parse_args(argv)
    summary = summarize(args.run_dir)
    print(f"{summary['successful_slots']}/{summary['expected_slots']} slots; reports: {args.run_dir / 'reports'}")


if __name__ == "__main__":
    main()
