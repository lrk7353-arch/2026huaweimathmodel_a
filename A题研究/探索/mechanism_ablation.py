#!/usr/bin/env python3
"""One predeclared case071/P2 attribution check; separate from search results."""
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "solver"))
from graph_ir import GraphIR
from plan import validate_plan
from common import DATA, OFFICIAL, atomic_json, digest
from evaluator import evaluate


def main():
    source = HERE / "runs" / "partition_v1"
    out = HERE / "runs" / "partition_v1_ablation"
    out.mkdir(parents=True, exist_ok=True)
    plans = source / "case_071" / "plans"
    critical = json.loads((plans / "critical_path_b20_communication_eft.json").read_text())
    stable = json.loads((plans / "stable_id_b20_communication_eft.json").read_text())
    assert all(not order for order in critical["core_schedules"][1:])
    assert all(not order for order in stable["core_schedules"][1:])
    # Preserve compute-op insertion order exactly; remove only subgraph labels
    # and priority buckets. The input graph and all operator-to-core labels stay.
    merged = {"node_to_subgraph": {op: 0 for op in critical["node_to_subgraph"]},
              "core_schedules": [[0], [], [], [], []]}
    conditions = [("merged_one_sg", merged), ("stable_id_20_sg", stable),
                  ("critical_path_20_sg", critical)]
    ir = GraphIR.from_path(DATA / "case_071.json")
    atomic_json(out / "manifest.json", {
        "script_sha256": digest(Path(__file__)), "source_exploration": str(source),
        "case": "case_071", "problem": 2, "hardware_cores": 5, "active_cores": 1,
        "conditions": [name for name, _ in conditions], "timeout": 60,
        "graph_sha256": digest(ir.path), "config_sha256": digest(DATA / "config.txt"),
        "official_py_sha256": {p.name: digest(p) for p in sorted(OFFICIAL.glob("*.py"))},
        "scope": "Supplementary attribution check, excluded from the 12-candidate search table"})
    rows = []
    for name, plan in conditions:
        validate_plan(ir, plan)
        assert set(plan["node_to_subgraph"]) == set(critical["node_to_subgraph"])
        atomic_json(out / (name + ".plan.json"), plan)
        record = evaluate(ir.path, plan, 2, out / "evaluations", timeout=60)
        row = {"condition": name, "subgraphs": len(plan["core_schedules"][0]),
               "record": record}
        rows.append(row)
        atomic_json(out / "results.json", rows)
        print(json.dumps({"condition": name, "status": record["status"],
                          "makespan": record.get("metrics", {}).get("makespan")}), flush=True)
    if all(r["record"]["status"] == "success" for r in rows):
        base, stable_time, critical_time = [r["record"]["metrics"]["makespan"] for r in rows]
        text = ["# 071/P2补充机制消融", "", "该实验不计入前述12候选搜索表。三条件均使用5个硬件核心列表，全部计算实际在核心0。",
                "", "|条件|子图数|Makespan|活跃核|新增DDR字节|", "|---|---:|---:|---:|---:|"]
        for row in rows:
            m = row["record"]["metrics"]
            text.append("|{}|{}|{}|{}|{}|".format(row["condition"], row["subgraphs"], m["makespan"],
                        m["active_cores"], m["data_movement_bytes"]["added_copy_bytes"]))
        text += ["", "合并为单sg时保留最佳计划的操作键插入顺序，只取消子图分桶，运行时间{}。".format(base),
                 "关键路径20块相对单sg降低{:.2%}，相对稳定ID20块降低{:.2%}。".format(1 - critical_time / base, 1 - critical_time / stable_time),
                 "这支持收益来自同核分块/优先序编码，而非增加活跃核或减少DDR字节。两种20块方案的块成员与优先级同时变化，不能严格称作保持分块不变、只交换块顺序的消融。",
                 "communication_eft使用1000/100周期的P1式静态代理生成计划；同一计划在P2的实测改善不证明该代理对P2时延预测准确。"]
        (out / "摘要.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
