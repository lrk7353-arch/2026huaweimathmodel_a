#!/usr/bin/env python3
"""Read-only live experiment progress. No solver imports or mutations."""
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print("  无法读取 {}：{}".format(path, error))
        return {}


def main():
    print("A题实验进度  " + datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"))
    print("这是当前磁盘记录；每次打开重新读取。无新结果不一定代表进程停止。\n")
    pause_receipt = ROOT / "实验记录/快速迭代调度调整_v1/暂停记录.json"
    if pause_receipt.exists():
        pause = read(pause_receipt)
        remaining = [row for row in pause.get("targets", []) if row.get("action") == "SIGSTOP_sent"]
        if remaining:
            print("【排期已调整】后续长队列暂缓，为快速试错留出算力。")
            print("  暂停记录中的父进程数：{}；实际存活状态见同目录恢复工具。".format(len(remaining)))
            print("  旧长队列保持暂停；当前新实验见“直接迭代”进度。")
            print("  下列4700是旧完整矩阵，不是每轮试错必须完成的工作。\n")
    quick_reports = sorted((ROOT / "实验记录").glob("快速开发轮_v*/summary.json"), key=lambda p: p.stat().st_mtime)
    if quick_reports:
        latest = quick_reports[-1]
        quick = read(latest)
        print("【最新快速开发轮】" + latest.parent.name)
        print("  12图完整验真：{}；实际调用 {}；耗时 {:.2f} 秒；耗时改善 {} 图。".format(
            quick.get("all_12_profiles_completed_and_verified"), quick.get("logical_calls"),
            quick.get("observed_wall_seconds_including_cli_and_audits", 0), quick.get("strict_makespan_improvement_count")))
        print("  这是固定开发面板上的局部精修，不代表全100图或大图用时。\n")
    direct = ROOT / "直接迭代/查看进度.py"
    if direct.exists():
        import runpy
        print("【当前：直接迭代】")
        runpy.run_path(str(direct), run_name="__main__")
        print("\n下方为旧批次记录，不是当前直接迭代的完成率。\n")
    totals = {}
    main_done = 0
    for version, relative, target in (
        ("v2", "advanced_solver/runs/formal_v2", 3100),
        ("v3", "精修求解器/runs/formal_v3", 1600),
    ):
        root = ROOT / relative
        completed = 0
        print("【{}：{}】".format(version, "正式对照及稳定性实验" if version == "v2" else "强化版及稳定性实验"))
        found = False
        for folder in sorted(root.iterdir()) if root.exists() else []:
            if not folder.is_dir() or not (folder / "manifest.json").exists():
                continue
            report = folder / "summary.json"
            final = report.exists()
            if not final:
                report = folder / "progress.json"
            if not report.exists():
                continue
            data = read(report)
            rows = data.get("slots", [])
            if not rows:
                continue
            found = True
            done = sum(bool(row.get("feasible") and row.get("search_completed")) for row in rows)
            planned = data.get("planned_slots", len(rows))
            failed = sum(row.get("outcome") not in (None, "pending", "completed_feasible", "success") for row in rows)
            completed += done
            if version == "v2" and folder.name in {"full_p1_seed17", "full_p2_seed17", "full_p3_seed17"}:
                main_done += done
            status = "已完成" if final and done == planned and data.get("source_hashes_unchanged") is True else "尚未完成"
            if failed or (final and not all(data.get(k) is True for k in ("all_slots_feasible", "all_searches_completed", "source_hashes_unchanged"))):
                status = "需检查"
            timestamp = datetime.fromtimestamp(report.stat().st_mtime).strftime("%m-%d %H:%M:%S")
            print("  {}：{}/{}，{}，失败配置 {}，更新 {}".format(folder.name, done, planned, status, failed, timestamp))
        if not found:
            print("  尚无已报告批次，等待前置实验。")
        print("  已完成实验配置 {}/{}；剩余 {}（包含未启动批次）".format(completed, target, target - completed))
        totals[version] = completed
        for file in sorted((root / "queues").glob("*.needs_review.json")):
            print("  队列需检查：" + str(file))
        print()
    print("当前v2主批次：{}/1200（{:.1f}%），剩余 {}".format(main_done, main_done / 12, 1200 - main_done))
    print("原预定v2/v3实验：{}/4700；重复方法/种子不等于新的最终方案。".format(sum(totals.values())))
    four = ROOT / "实验记录/正式P3四格_v2"
    report = four / "summary.json"
    if not report.exists():
        report = four / "progress.json"
    if report.exists():
        data = read(report)
        done = data.get("four_cell_complete_count", data.get("complete_four_cells", 0))
        print("旧P3全核交叉验证队列：{}/400组；阶段 {}".format(done, data.get("phase", "已汇总")))
    else:
        print("旧P3全核交叉验证队列：等待前置实验；本轮五核结果见上方直接迭代。")
    status_path = ROOT / "实验记录/完整计算交付收尾_v3/status.json"
    if status_path.exists():
        status = read(status_path)
        labels = {"waiting": "等待实验齐全", "running": "执行中", "complete": "已完成", "needs_review": "需检查", "failed": "失败", "timed_out": "已超时", "source_changed": "源文件变化，已停止"}
        print("旧完整交付队列：" + labels.get(status.get("state"), str(status.get("state"))))
        if status.get("current_stage"):
            print("  当前阶段：" + status["current_stage"])
        if status.get("error"):
            print("  错误：" + status["error"])
    print("\n1500 = 100张图 × 3个场景 × 5种核数，是最终方案覆盖数。")
    print("上面的实验配置包括对照与多种子，会重复处理同一组合，不能当作1500份方案的完成率。")


if __name__ == "__main__":
    main()
