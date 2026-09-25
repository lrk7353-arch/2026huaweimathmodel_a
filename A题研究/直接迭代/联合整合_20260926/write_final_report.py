"""Write the final evidence-based report after the registered gate and replay."""
import csv,json,sys
from pathlib import Path
HERE=Path(__file__).resolve().parent
def csvread(p):
    with p.open(encoding="utf-8-sig") as f:return list(csv.DictReader(f))
def main():
    panel=HERE/"同预算对照";decision=json.loads((panel/"晋级判定.json").read_text())
    assert not decision["run_final1500"],"Successful full gate requires executing the conditional final campaign first."
    groups=csvread(panel/"分组汇总.csv");curves=csvread(HERE/"最终累计/各核数汇总.csv")
    current={int(r["problem"]):float(r["mean_speedup"]) for r in curves if r["cores"]=="5"}
    imported=json.loads((HERE/"累计并集/summary.json").read_text())
    winners=json.loads((HERE/"最终累计/summary.json").read_text())
    execution=json.loads((panel/"execution.json").read_text())
    attempted=json.loads((HERE/"新增精选复评/结果.json").read_text())
    lines=["# 联合整合执行结果","",
      "已完成重点版本标记、双方历史合并、完整方案并集、111项导入复评、整合控制器实现、216实验臂同预算比较及新增胜例复评。按照事先登记的门槛，本版整合控制器未获全场景晋级，不替换既有默认入口，也不启动条件未满足的1500从头全量。",
      "",
      "本阶段交付的是可直接使用的累计精选库、可运行的成熟主干入口和保留正负例的整合实验模块。不存在新的统一从头1500成绩。",
      "",
      "## 重点版本与Git","",
      "- 回滚标签：refs/tags/milestone/pre-integration-20260925，固定于238aafe，本地/GitHub均可用。",
      "- 当前分支：codex/joint-integration-v3；草稿PR：https://github.com/lrk7353-arch/2026huaweimathmodel_a/pull/4。",
      "- 14b91b6保留双方Git祖先；2362dd7冻结实验源码和协议。main未合并。",
      "- 同名P1模块分别保留为p1_joint_regions与p1_local_regions，历史入口兼容；103项测试通过。",
      "",
      "## 当前可交付累计成绩","",
      "下表均为五核、100图，逐图以原官方单核时间除以方案时间后取均值。不是官方总分，也不是固定预算从头成绩。",
      "",
      "|问题|整合前我方|队友第十三轮|初始并集|接收本轮新胜例后|",
      "|---|---:|---:|---:|---:|"]
    old={1:3.6653494314097714,2:4.388427246682187,3:4.482579350414788}
    team={1:3.736824464904486,2:4.505904948994485,3:4.621353380071978}
    union={1:3.783705404827019,2:4.535467166592133,3:4.65487832087166}
    for p in (1,2,3):lines.append(f"|P{p}|{old[p]:.8f}|{team[p]:.8f}|{union[p]:.8f}|{current[p]:.8f}|")
    lines.extend(["",f"初始1500份并集全部结构合法；111项导入独立复评111/111一致。随后从本次strong和joint两臂取出{winners['fresh_additions']}项新累计胜例，独立复评后纳入，其中严格降时{winners['strict_time_additions']}项、等时减COPY {winners['same_time_copy_additions']}项。其余项目继承既有官方成功记录，没有声称本轮重评全部1500份。",
      "",
      "完整方案、逐图账本、1—5核均值与15个压缩分包见最终累计/。每份计划保留来源提交、精确顺序和SHA256。初始累计并集/保留作阶段快照。",
      "52项严格降时中，P1有23项、P2有14项、P3有15项。这些来自强主干与整合版的共同搜索，不能全部归功于新控制器。",
      "",
      "## 同预算算法比较","",
      "每臂B16/T180、单次45秒，全新评测目录。strong为P1 integrated或P2/P3 trace_routed；joint保留B12强前缀，余额执行互补候选。12张开发图、12张验证图、4张图的2—4核，共108配置、216实验臂。",
      "",
      "|组别|问题|joint胜/平/负|平均配对降时|最坏退步|",
      "|---|---|---|---:|---:|"])
    for g in groups:
        label={"development":"开发","validation":"验证","lowcore":"低核"}[g["group"]]
        lines.append(f"|{label}|P{g['problem']}|{g['joint_wins']}/{g['ties']}/{g['strong_wins']}|{float(g['mean_paired_reduction_pct']):+.4f}%|{float(g['worst_regression_pct']):.4f}%|")
    lines.extend(["",
      "正数表示joint更快。门槛在结果前登记：每场景验证均值至少+0.5%、至少3胜、最坏退步不超过3%、低核均值不退步且有效性通过；三场景全部通过才运行最终1500。详见执行协议.json与同预算对照/晋级判定.json，不事后改门槛。",
      "",
      "P2、P3分别通过场景门槛，P1未通过，因此整体不晋级。合并三组看，P1为0胜27平9负，P2为14胜22平0负，P3为15胜20平1负；P3在开发图044存在显著退步，不能只引用验证组无负例。验证组中，降时至少0.5%的图数分别为0、4、5，严格胜出并非都属于明显改善。",
      "",
      "求解成本也有增加：各场景两臂实际求解墙钟时间之和比较，joint较strong增加P1 5.09%、P2 22.83%、P3 24.80%。这来自六进程并发批次，不能视作隔离环境的纯运行效率基准；但本轮没有证据支持其更省求解时间。候选覆盖改善尚未转化为稳定的全场景效率突破。",
      "",
      "两臂调用上限相同，少数大图先达到软墙钟上限，实际调用数可能较少。候选生成不可中断，因此少数运行略超180秒；保留实际值。B8/B12/B16表是一次B16运行的已执行前缀，不是三次独立启动。",
      "",
      "## 正负例揭示了什么","",
      "009/P2：强主干B16为52200，joint前12次也为52200，尾部降到51517。保留强路径修复了先前的候选饥饿；后续683周期来自region_observed_order_control，即重新编码/观测顺序对照，并非迁移或缓存收益。",
      "",
      "075/P1：两边第12调用均760157。strong继续既有联合邻域，第13—15调用降到616955；joint改试互补候选，最终732513，慢18.73%。044/P3也在强主干第16调用找到68361，而joint为80374，慢17.57%。固定保护12次只是把预算截断位置后移，不能保证强路线后四次没有价值。",
      "",
      "006/P2/P3：本次joint从头均31729，没有复现已有20866/20505的精选优势。这表明历史好方案接收成功，不等于结构路由与有限预算已能自动重现它。051/P1的成熟主干本次达到259576，明显好于原我方290405，应归到成熟主干及累计成果，不能算joint独有收益。",
      "",
      "尾部仅剩4次调用，旧队列的两次续跑配额和刷新迟滞主要由单元契约保证，本轮不能声称已单独实证其普遍收益；本轮最直接验证的是强前缀保护与余量候选的整体取舍。",
      "",
      "## 薄弱图研究","",
      "额外4次官方复评显示：064与047的固定Task必要下界占当前周期约90.2%与85.6%；044的最忙核单管线忙碌占77.2%；069固定方案边界DDR下界占75.2%。因此分别关注Task等待结构、负载重分配、边界聚合与复用，不能统一细切。下界仅针对既定方案，不是全局最优界。",
      "",
      "joint已接入沿实际完工阻塞链选择区域、跨旧Task重划的候选；此次对照没有支持将整套控制器升级默认。详细诊断和原始成功记录见薄弱图诊断/。",
      "",
      "## 成本与收尾","",
      f"主对照全部{execution['completed']}/{execution['expected']}实验臂完成，{execution['new_calls']}次新官方调用，候选失败/超时共{execution['failures']}次，批次墙钟{execution['elapsed_seconds']:.2f}秒。导入复评111次，薄弱图诊断4次，新增胜例独立复评{attempted['attempted']}次；本阶段这些可核对调用合计{execution['new_calls']+111+4+attempted['attempted']}次。历史库构建成本不计为本次免费搜索成绩。",
      "",
      "交付核验通过：176份冻结源码哈希未变，111份官方输入及评测文件与双方原始版本一致，最终1500项覆盖和哈希正确，15个分包均可精确还原。3376次搜索调用均为新调用，3360次成功、16次超时；全部216臂都保留了有效最优方案。详见交付核验.json。",
      "",
      "实验结果完整保存。成熟主干使用joint_solver.py --variant strong；整合实验使用solve.py --engine joint --from-scratch。按批准的条件到此完成本轮整合决策，不继续调控制器，不启动未通过门槛的全量实验。",
      "",
      "复现命令与回滚操作见使用与回滚.md；逐臂、逐配置、预算前缀及全部调用记录见同预算对照/。"])
    (HERE/"执行结果.md").write_text("\n".join(lines)+"\n")
    print(json.dumps(dict(current_fivecore=current,winners=winners,gate=decision,execution=execution),ensure_ascii=False,indent=2))
if __name__=="__main__":main()
