"""Write the first work-cycle report using actual saved results and hashes."""
from collections import Counter
from datetime import datetime
from pathlib import Path
import sys

sys.dont_write_bytecode=True
RESEARCH=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(RESEARCH/"solver"))
from common import ROOT, DATA, OFFICIAL, atomic_json, digest, read_json
from summarize import summarize


def link(label,path):return f"[{label}]({Path(path).resolve()})"


def main():
    full_dir=RESEARCH/"solver/runs/full_initial_v1"
    pilot_dir=RESEARCH/"solver/runs/pilot_fullpool_v2"
    full=summarize(full_dir)
    pilot=summarize(pilot_dir)
    verified=read_json(RESEARCH/"实验记录/independent_heft_check/verification.json")
    old=read_json(RESEARCH/"方案审阅/结构核验/a_structure_summary.json")
    graph_checks={name:digest(DATA/name)==sha for name,sha in old["input_sha256"].items()}
    provenance=read_json(RESEARCH/"方案审阅/cache_microtests/provenance.json")
    official_checks={name:digest(ROOT/name)==sha for name,sha in provenance["files_sha256"].items()}
    status=Counter()
    timeout_cases=[]
    for path in full_dir.glob("evaluations/attempts/*/record.json"):
        r=read_json(path);status[r["status"]]+=1
        if r["status"]=="timeout":timeout_cases.append({"graph":Path(r["graph_path"]).stem,"problem":r["problem"],"record":str(path)})
    lines=["# A题推进报告：第一轮工程与研究结果", "", f"更新：{datetime.now().strftime('%Y-%m-%d %H:%M')}（本机北京时间）。用户确认还剩四天；本报告是第一轮交付，不是最终参赛论文。", "",
           "**已建立可复现求解与官方评测工程，并在四张困难图上找到经过独立复核的操作级分核改进。** 后续重点是把这条方向推广到全部核数与全部图，再做同预算比较、局部搜索与消融。", "",
           "## 1. 实际完成范围", "",
           "- 标准库求解与评测入口已实现：Graph IR、简单/共享输入装箱、候选择优、三个场景及官方单核基准、超时日志、结果缓存、续跑、CSV与报告。",
           "- 核心42项测试在Python 3.9.6与3.12.14通过；覆盖机制、合法性、缓存、错误处理与结果聚合。",
           "- 实验性P2/P3统一入口已支持1—5核，额外12项测试通过；5条真实命令覆盖全部核数，共25份候选成功。尚未完成该入口的全100图及同预算验证。",
           "- 该入口额外完成全100图×1—5核的500槽结构验收，4364份候选均通过结构检查、无生成失败；此项未调用官方执行模拟，不能算作4364份真实可执行成绩。",
           f"- 9张代表图、两种基线、1/2/5核、P1/P2/P3：{pilot['successful_slots']}/{pilot['expected_slots']}个求解结果槽，{pilot['candidate_status_counts'].get('success',0)}条成功候选记录（包含缓存复用）。",
           f"- 全100图初版：官方整图单核基准{full['baseline_successes']}/100；P1/P2×2/5核共{full['successful_slots']}/400个成功结果槽。每槽最多4候选，是有限预算初版，尚未覆盖3/4核及全100图P3。",
           f"- 原始图{sum(graph_checks.values())}/{len(graph_checks)}、官方配置及代码{sum(official_checks.values())}/{len(official_checks)}哈希与前次核验一致。", "",
           link("基线求解器说明",RESEARCH/"solver/README.md")+" · "+link("实验性统一入口",RESEARCH/"探索/README.md")+" · "+link("四天安排",RESEARCH/"四天推进安排.md")+" · "+link("方法与实验设计草稿",RESEARCH/"论文草稿/方法与实验设计.md"), "",
           "## 2. 全100图首轮基线", "",
           "P1/P2加速比为官方整图单核时间除以多核时间，按逐图比值算术平均。只有完整覆盖100图的分组才显示平均值，未齐全不以成功子集代替。", "",
           "| 场景 | 配置核数 | 有效用例 | 平均加速比 |", "|---|---:|---:|---:|"]
    for row in full["aggregates"]:
        value=f"{row['mean_speedup']:.4f}倍" if row["mean_speedup"] is not None else "待齐全"
        lines.append(f"| P{row['problem']} | {row['num_cores']} | {row['valid_cases']}/100 | {value} |")
    full_figure=RESEARCH/"实验记录/figures/全100图结构与基线表现.png"
    if full.get("complete") and full_figure.exists():
        lines += ["", f"![全100图P2结构与实际加速比分布；2核和5核]({full_figure})"]
    lines += ["", link("逐图结果与完整基线报告",full_dir/"reports/首轮基线实验报告.md"), "",
              f"全量run的评估调用状态（含基准与缓存调用）：`{dict(status)}`。超时尝试全部保留，不代表方案在数学上不可行。部分大图超过300秒；单核参照在独立校准或较长时限的恢复队列中得到成功结果后，经完整键及gzip哈希核验导入，保留cache_imports及原失败记录。恢复官方单核参照不替换成其他优化单核方案。",
              "当前调用耗时混合冷计算与成功缓存复用，不能用于宣称不同方法的冷启动求解速度优势。正式耗时比较另设独立空缓存。", "",
              "## 3. 最值得继续投入的方向：操作级分核", "",
              "连续拓扑块先导验证了P2的分块/优先序可以有效，但纯粹按连续区间切块仍会损害并行分支。进一步按操作依赖、每核M/V可用时间、张量路由和跨核同步代价生成分核方案，得到以下结果。", "",
              "下表单位为周期。P2/P3的8次独立复核全部从空缓存运行；四个P2成绩完全复现。整分量基线是该图已评估的WCC方法结果，不能代替官方整图单核参照。", "",
              "| 图 | 整分量P2 | 连续块P2 | 操作级P2 | 相对整分量降时 | 同方案P3 |", "|---|---:|---:|---:|---:|---:|"]
    for row in verified["cases"]:
        b=row["wcc_baseline"];p2=row["evaluations"]["2"]["metrics"]["makespan"];p3=row["evaluations"]["3"]["metrics"]["makespan"]
        lines.append(f"| {row['case'][-3:]} | {b:,} | {row['partition_baseline']:,} | {p2:,} | {(1-p2/b)*100:.2f}% | {p3:,} |")
    lines += ["", "这里的操作级最好方案均实际使用5核。早期071的连续块方案只用1核就从18919改善到14656，来自同核分块与优先序；当前10051/8212是另一个真正使用5核的方案，两种收益不能混称。", "",
              "这四图是预选困难代表，不是全100图均值，也尚未完成同预算、多种子和完整消融。HEFT式EFT本身是已知启发式思路，不能单凭采用它就声称创新；研究贡献应落在本题机制适配、联合优化与充分证据上。", "",
              "代价也必须保留：049操作级方案新增DDR搬运为5301640 B，虽然更快，仍需进一步减少关键通信；051则同时把连续块方案新增的7208996 B降到230808 B。", "",
              link("操作级原型与限制",RESEARCH/"探索/runs/operation_heft_v1/机制解读.md")+" · "+link("独立复核",RESEARCH/"实验记录/independent_heft_check/README.md"), "",
              f"![四张困难图的实际改进路径；横轴为千周期，各面板尺度不同]({RESEARCH/'实验记录/figures/困难图改进路径.png'})", "",
              "**进一步的单轮局部调整已验证：**保持全局拓扑序，仅将071的操作720由核心3移到核心2，重新编码后P2从10051降到9792（下降2.58%），新增COPY减少4608 B；同一新方案P3为8190，比旧方案的8212再少22周期（0.27%）。两次均经独立空缓存复核。049仅在时间不变时减少60 B搬运，收益很小。此探针共两图、16个候选，不是完整多轮局部搜索。", "",
              link("局部通信探针及负结果",RESEARCH/"探索/runs/critical_transfer_v1/复核与局限.md")+" · "+link("071局部调整独立复核",RESEARCH/"实验记录/independent_local_move_check/README.md"), "",
              "## 4. 已排除的误导方向", "",
              "1. **P1/P2采用同样细分策略。** 四图连续块候选在P1全部更慢，原方案已保留。P1的Task切换、DDR中转代价不能照搬P2模型。",
              "2. **命中率越高越好。** case011循环错峰命中率升到50%，耗时却从46880恶化到52607；case093有L2节省105周期，但重排总收益仍为负。这两图均保留原方案。",
              "3. **早期缓存无收益意味着缓存无用。** 原WCC方案的108组固定方案对照收益为1；新的五核操作级方案在071中加入L2却从10051降至8212，降低18.30%。机会取决于分核和关键读取时刻。",
              "4. **超过核数的加速比一定是错误。** 核内启发式编排与spill可能变化，必须核对实际官方输出，不能人为截断到核数。", "",
              link("缓存时间线诊断",RESEARCH/"实验记录/P3缓存收益诊断.md")+" · "+link("真实图错峰负结果",RESEARCH/"solver/runs/cache_reorder_v1/P3重排探针报告.md"), "",
              link("071新分核的固定方案P2/P3时间线",RESEARCH/"实验记录/figures/071固定方案缓存时间线.png")+"：两面板使用同一时间轴、同一方案，所有操作区间取自独立复核的官方结果；五个核心的结束时刻均提前。此图不是对单个命中事件的因果归因。", "",
              "## 5. 下一轮按什么顺序推进", "",
              "1—5核的实验性统一候选入口已建立，保留已验证方案并记录预算。下一步先验证其全量表现，再围绕关键同步、同核优先序、内存与通信代价设计局部搜索。P3以新分核为基础，只针对关键读取研究缓存。补齐全量核数和场景后，冻结规则，做同预算、多种子与消融。", "",
              link("下一轮任务与验收",RESEARCH/"下一轮研究任务.md")+" · "+link("正式实验协议",RESEARCH/"正式实验协议_v1.md")+" · "+link("当前已验证最好方案目录",RESEARCH/"当前最佳方案/README.md"), "",
              link("依据全100图实际成绩生成的优先排查名单",RESEARCH/"实验记录/下一轮优先图/README.md")+"：用实际时间相对计算/DDR/纯计算路径宽松下界的差距排序。名单用于诊断先后，不按题号写特制规则，也不保证下界可以达到。", "",
              "当前最好方案目录只是对已评估方案取优的档案，每份方案有完整来源。它不是一个独立计时的新算法，也不代表全局最优。", "",
              "图表由保存的原始CSV与独立复核JSON生成，PNG/PDF及来源元数据均留档；已检查字体、标签、坐标和边界。可视化工作流参考与核验日期见论文结构文档的软件说明，不作为调度算法性能证据。", ""]
    output=RESEARCH/"推进报告_第一轮.md"
    output.write_text("\n".join(lines),encoding="utf-8")
    atomic_json(RESEARCH/"实验记录/第一轮报告核验.json",{"graph_integrity":graph_checks,"official_integrity":official_checks,
                "full_summary_sha256":digest(full_dir/"reports/summary.json"),"pilot_summary_sha256":digest(pilot_dir/"reports/summary.json"),
                "heft_verification_sha256":digest(RESEARCH/"实验记录/independent_heft_check/verification.json"),
                "local_move_verification_sha256":digest(RESEARCH/"实验记录/independent_local_move_check/verification.json"),
                "advanced_structure_sha256":digest(RESEARCH/"实验记录/advanced_structure_audit/summary.json"),
                "timeout_attempts":timeout_cases,"report_sha256":digest(output)})
    print(output)


if __name__=="__main__":main()
