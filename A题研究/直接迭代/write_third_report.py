"""Write the third-round report from completed official records and paired data."""
import argparse
import shutil
from collections import Counter
from common_run import *


def main(out):
    out = Path(out)
    runs = R / '直接迭代/运行结果'
    portfolio = read_json(out / 'summary.json')
    quality = read_json(runs / '深化等预算_v1/analysis.json')
    before = {key(r): r for r in read_json(runs / '深化计划_v1/before.json')['records']}
    batches, slots, trials, skipped = [], [], [], []
    for name in ('深化任务粒度_v1', '深化任务跨核_v1'):
        folder = runs / name
        batch = read_json(folder / 'summary.json')
        assert batch['complete'] and batch['completed'] == batch['expected']
        local_trials, local_skips = [], []
        for p in sorted(folder.glob('slots/*/*/summary.json')):
            s = read_json(p)
            winner = next((t['name'] for t in reversed(s['evaluations']) if t['accepted']), 'incumbent')
            slots.append(dict(batch=name, case=s['case'], cores=s['num_cores'], before=s['before'], after=s['after'],
                              reduction_pct=100*(1-s['after']/s['before']), winner=winner,
                              logical_calls=s['logical_calls'], fresh_calls=s['new_calls'],
                              elapsed_seconds=s['elapsed_seconds'], stop_reason=s['stop_reason']))
            for t in s['evaluations']:
                rec = t['record']
                row = dict(batch=name, case=s['case'], cores=s['num_cores'], name=t['name'],
                           status=rec['status'], makespan=rec.get('metrics', {}).get('makespan'),
                           accepted=t['accepted'], cache_hit=rec['cache_hit'], **t['metadata'],
                           official_record=rec['record_path'])
                local_trials.append(row)
            local_skips += [dict(batch=name, case=s['case'], cores=s['num_cores'], **t) for t in s['skipped']]
        trials += local_trials
        skipped += local_skips
        rr = batch['rows']
        batches.append(dict(batch=name, slots=len(rr), improved=sum(r['after'] < r['before'] for r in rr),
                            logical_calls=sum(r['logical_calls'] for r in rr), fresh_calls=sum(r['new_calls'] for r in rr),
                            wall_seconds=batch['wall_seconds'], statuses=dict(Counter(t['status'] for t in local_trials)),
                            skipped=dict(Counter(t['reason'] for t in local_skips))))
    write_csv(out/'任务精修配置.csv', slots)
    write_csv(out/'任务精修候选.csv', trials)
    write_csv(out/'任务精修剪枝.csv', skipped)
    atomic_json(out/'实验汇总.json', dict(quality=quality, task_batches=batches))
    for source, dest in [('results.csv','同预算全部配置.csv'), ('按图比较.csv','同预算按图比较.csv'), ('种子差异.csv','同预算种子差异.csv')]:
        shutil.copyfile(runs/'深化等预算_v1'/source, out/dest)
    with (out/'改善清单.csv').open(encoding='utf-8-sig') as f:
        improvements = list(csv.DictReader(f))
    examples = []
    for case, n in [('case_062',5), ('case_063',5), ('case_063',3), ('case_014',5), ('case_072',5)]:
        row = next(r for r in improvements if r['case']==case and int(r['cores'])==n and r['problem']=='1')
        examples.append(f"| {case} | {n} | {int(row['before']):,} | {int(row['after']):,} | {float(row['reduction_pct']):.2f}% |")
    mechanism = []
    for case in ('case_062', 'case_063'):
        b = before[case,1,5]
        new = read_json(runs/'深化任务粒度_v1/slots'/case/'p1_n5/summary.json')['best_record']
        old_count = len(set(read_json(b['plan_path'])['node_to_subgraph'].values()))
        new_count = len(set(read_json(new['plan_path'])['node_to_subgraph'].values()))
        mechanism.append(f"- {case}：保持算子所属核心，Task 数 {old_count}→{new_count}；新增 COPY 字节 {score(b)[1]:,}→{score(new)[1]:,}。这是任务边界减少伴随的实测变化，不能把全部周期收益都归因于 COPY。")
    means = '\n'.join(f"| P{g['problem']} | {g['before_mean_speedup']:.4f} | {g['after_mean_speedup']:.4f} | {g['strict_time_improved']} |"
                      for g in portfolio['groups'] if g['cores']==5)
    batch_table = '\n'.join(f"| {b['batch']} | {b['slots']} | {b['improved']} | {b['logical_calls']} / {b['fresh_calls']} | {b['statuses'].get('timeout',0)} | {b['wall_seconds']:.1f}秒 |" for b in batches)
    compare_table = '\n'.join(f"| {m} | {c['wins']} / {c['ties']} / {c['losses']} | {c['mean_paired_time_reduction_pct']:.2f}% |" for m,c in quality['comparisons'].items())
    text = f'''# 第三轮：P1任务粒度优化与同预算对照

2026年9月25日。论文继续暂缓；本轮改进求解器、验证收益并直接输出完整方案。

## 这轮交付了什么

以第二轮结束时保存的1500份方案为比较起点，本轮累计有 **{portfolio['strict_time_improved']} 个配置进一步降低官方计算图耗时**，目标值改善 {portfolio['objective_improved']} 个，保留方案耗时回退 {portfolio['regressions']} 个。现有覆盖仍为 **1500/1500**，新增覆盖 {portfolio['new_coverage']}；完整覆盖不是全局最优证明。

方案在[方案目录](方案)，成绩在[全部成绩](全部成绩.csv)，本轮变化在[改善清单](改善清单.csv)。比较起点已经包含第二轮389个配置的降时，因此不会重复计入本轮收益。成绩单位是官方模拟周期，不是Python运行秒数。

| 图 | 核数 | 本轮前周期 | 本轮后周期 | 耗时降低 |
|---|---:|---:|---:|---:|
{chr(10).join(examples)}

全100图的五核平均加速比（对原官方单核基准逐图取比值，再算术平均）：

| 场景 | 本轮前 | 本轮后 | 五核降时配置数 |
|---|---:|---:|---:|
{means}

这张表是不同历史预算累积的最好方案库，不能作为统一预算的算法排名。P2/P3本轮没有新增搜索，保留此前成果。

## 方法为什么有效

P1现有方案有两类相反的问题：Task过碎时，边界搬运和等待增多；Task过大时，内部排程与共享带宽的相互作用可能不利。新增 `p1_task_refine.py`，一次从同一个官方可行方案生成三类候选：

1. **同核任务合并**：在“数据依赖＋原核心顺序”的DAG上作拓扑排序，只合并拓扑序中同核连续块，防止收缩后形成环；算子所属核心不变。新合并任务尝试256、512、1024、2048个算子上限，原本超大的任务单独保留。
2. **大任务拆分**：按原弱连通分量与局部拓扑序拆成至多1024或512个算子的Task，保持所属核心；适合过大的原任务。新增边界可能增加搬运，所以必须实评择优。
3. **实测时间重排**：保持Task划分，分别用官方局部排程时长和实际持续时间估计任务成本，按后继关键路径长度排优先级、最早完成时间选择核心。旧负载下的持续时间只是启发信息，不能替代新排程的评测。

必要Task下界与必要DDR搬运下界取最大值（不相加），严格超过当前最好周期数才剪枝。其余候选调用未修改的官方评测器；只接受周期更低，或周期相同且新增COPY更少的成功结果。单个候选超时则保留旧方案。现阶段是固定输入方案的一层邻域搜索，并未宣称找到局部或全局最优。

{chr(10).join(mechanism)}

## 这轮实际花了多少计算

| 批次 | 配置数 | 降时配置 | 逻辑调用 / 新调用 | 超时候选 | 批次墙钟时间 |
|---|---:|---:|---:|---:|---:|
{batch_table}

第一批是人工选取的12个困难P1五核配置，每配置最多6次调用、150秒软预算；第二批是首批合并收益明显的4图×2/3/4核，每配置最多2次、50秒软预算，属于有针对性的扩展，不能作为无偏全图胜率。候选生成、加载、写盘计入时间，截止点在候选边界检查；少量超出软预算是可能的。候选池耗尽或下界剪枝也会少于调用上限。

所有尝试和负例见[候选表](任务精修候选.csv)、[剪枝表](任务精修剪枝.csv)、[配置表](任务精修配置.csv)。首批48次调用中5个超时候选并未获得成绩，不能把超时解释成劣解；另有评测成功但更慢的候选，都没有替换已有结果。case_016在本候选池中没有改善，不代表它已最优。

便携单图入口也已实跑：从第二轮case_062五核方案出发，先复评输入，再做1个精修候选；两次逻辑调用得到942950周期。这个检查使用了已有候选缓存，不用于宣称冷启动速度。

## 独立的同调用上限对照

预先按图结构分成“小/中图×重分量/分散分量”四层，各抽最多4张，共16图（计算算子≤6000），使用种子17、42、73，五核P1，每方法每种子最多8次逻辑官方调用。选择未看算法成绩；没有使用历史最好方案作为起点。三方法均包含完整弱连通分量候选：component专注整分量，legacy追加旧粗切分，adaptive追加结构选择性切分与必要下界剪枝。

每张图先取三种子耗时中位数，再逐图配对比较：

| adaptive比较对象 | 胜 / 平 / 负（按图） | 平均配对耗时降低 |
|---|---:|---:|
{compare_table}

**强Component仍须保留**：case_074中component为332092、adaptive为334798；case_100分别为57811、57971。当前短预算候选分配没有覆盖所有整分量优势，因此不能说adaptive全面优于强基线。组合择优有必要，但若比较组合算法，还需把组合的总调用预算统一。

144个配置全部有可行结果，逻辑调用{quality['logical_calls']}次、实际新调用{quality['fresh_calls']}次，评测超时{quality['timeouts']}次，观测批次墙钟{quality['batch_wall_seconds']:.1f}秒。**共享了官方结果缓存，该时间不是冷启动算法运行时间，也不是等墙钟预算比较。** 单次评测上限60秒，单配置总时间上限未作为实验约束；“同预算”仅指相同的最多8次逻辑调用。

三种子并非独立的48张图：component在12/16图、legacy在13/16图、adaptive在14/16图上的获胜方案完全相同。因此只报告16图配对结果和实际方案差异，不虚增样本量或给出独立样本置信区间。范围也不覆盖最大图。

详见[逐图比较](同预算按图比较.csv)、[种子差异](同预算种子差异.csv)和[144项原始成绩](同预算全部配置.csv)。新增任务粒度算法属于追加预算实验，尚未进入这个同调用上限对照，不能将其收益混入上表。

## 如何继续用

单图入口：[solve.py](../solve.py)，使用 `--p1-refinement tasks`。完整命令见[运行说明](../README.md)。指定 `--incumbent-plan` 可跨电脑运行：只依赖官方图、官方代码和输入方案，先官方复评再精修，不需要原电脑的历史实验目录。

下一步优先把任务粒度优化纳入从头求解的候选预算，与强Component在相同总调用限制下比较；其次诊断case_016这类依赖密集大图，分析可达下界与当前解的差距。P3的缓存路径需要独立机制证据，不能借用本轮P1收益。暂不恢复旧长队列。

导出后已核对1500个唯一配置、方案两字段格式/核心数、官方成功记录及成绩对应关系、方案与被评测文件逐字节一致；对22个目标值改善方案额外检查依赖图合法性。新增算法的3项测试通过（含随机DAG、跨核绕路不得错误合并、核心归属保持），便携入口实跑通过；没有为此重跑1500次评测。

本目录可直接使用；CSV中的原始官方记录路径仍指向本机历史实验目录，换电脑可用方案进行官方复评。本轮代码、结果尚未自动Git提交。
'''
    (out/'第三轮结果.md').write_text(text, encoding='utf-8')
    print(json.dumps(dict(output=str(out/'第三轮结果.md'), batches=batches), ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    main(parser.parse_args().out)
