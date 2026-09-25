"""Collect completed fourth-round experiments without rerunning evaluations."""
import argparse,shutil,statistics
from collections import Counter
from common_run import *


def main(out):
    out=Path(out);runs=R/'直接迭代/运行结果';portfolio=read_json(out/'summary.json')
    comparisons=[];panels=[]
    for name,label in [('融合开发_v1','开发16图'),('融合扩展_v1','扩展8图')]:
        a=read_json(runs/name/'analysis.json');panels.append(dict(name=name,label=label,**a))
        for src,dest in [('按图比较.csv','按图比较.csv'),('种子差异.csv','种子差异.csv'),('results.csv','全部配置.csv')]:
            shutil.copyfile(runs/name/src,out/(label+'_'+dest))
        for method,c in a['comparisons'].items():comparisons.append(dict(group=label,reference='hybrid',control=method,**c))
    write_csv(out/'同预算比较.csv',comparisons)
    batches=[];trials=[];skips=[]
    for name in ['融合张量精修_v1','融合张量跨核_v1','融合张量细化_v1','融合张量跨核细化_v1']:
        s=read_json(runs/name/'summary.json');assert s['complete']
        start=len(trials)
        for p in sorted((runs/name).glob('slots/*/*/summary.json')):
            slot=read_json(p)
            for t in slot['evaluations']:
                rec=t['record']
                trials.append(dict(batch=name,case=slot['case'],cores=slot['num_cores'],name=t['name'],
                    status=rec['status'],makespan=rec.get('metrics',{}).get('makespan'),accepted=t['accepted'],
                    cache_hit=rec['cache_hit'],official_record=rec['record_path'],**t['metadata']))
            skips += [dict(batch=name,case=slot['case'],cores=slot['num_cores'],**t) for t in slot['skipped']]
        batches.append(dict(batch=name,configs=len(s['rows']),improved=sum(r['after']<r['before'] for r in s['rows']),
            logical_calls=sum(r['logical_calls'] for r in s['rows']),fresh_calls=sum(r['new_calls'] for r in s['rows']),
            wall_seconds=s['wall_seconds'],statuses=dict(Counter(t['status'] for t in trials[start:]))))
    write_csv(out/'张量分区候选.csv',trials);write_csv(out/'张量分区剪枝.csv',skips)
    diagnosis=read_json(runs/'融合张量结构_v1/case016_diagnosis.json')
    atomic_json(out/'case016诊断.json',diagnosis)
    replay=read_json(runs/'融合独立复评_v1/summary.json')
    assert replay['best_record']['status']=='success' and replay['best_record']['metrics']['makespan']==3304638
    assert replay['new_calls']==1
    atomic_json(out/'实验汇总.json',dict(panels=panels,batches=batches,independent_replay={
        'makespan':3304638,'fresh_calls':1,'elapsed_seconds':replay['elapsed_seconds'],
        'record_path':replay['best_record']['record_path']},
        scope='same-call-cap small/medium panels and additional-budget targeted tensor-region research are separate'))
    with (out/'全部成绩.csv').open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
    examples='\n'.join(f"| {r['cores']} | {int(r['before']):,} | {int(r['after']):,} | {float(r['reduction_pct']):.2f}% |"
                       for r in rows if r['case']=='case_016' and r['problem']=='1' and int(r['cores'])>1)
    means='\n'.join(f"| P{g['problem']} | {g['before_mean_speedup']:.4f} | {g['after_mean_speedup']:.4f} |"
                     for g in portfolio['groups'] if g['cores']==5)
    comparison_table='\n'.join(f"| {r['group']} | {r['control']} | {r['wins']} / {r['ties']} / {r['losses']} | {r['mean_paired_time_reduction_pct']:.2f}% |" for r in comparisons)
    batch_table='\n'.join(f"| {b['batch']} | {b['configs']} | {b['logical_calls']} | {b['wall_seconds']:.1f} |" for b in batches)
    text=f'''# 第四轮结果：统一预算验证与大图突破

2026年9月25日。当前累计最好方案仍覆盖1500/1500；相对第三轮结束时，本轮 **{portfolio['strict_time_improved']}个配置降时**、目标值改善{portfolio['objective_improved']}个、保留方案耗时回退{portfolio['regressions']}个。论文仍暂缓。本轮实验均已结束，旧长队列未恢复。

最新输出：[1500份方案](方案)、[全部成绩](全部成绩.csv)、[改善清单](改善清单.csv)。所有成绩均来自原官方评测器，单位为计算图模拟周期；它与求解代码运行秒数不同。

## case_016的突破

| 核数 | 第三轮周期 | 第四轮周期 | 降低 |
|---:|---:|---:|---:|
{examples}

五核路线：7,715,523 → **4,026,098**（按张量切分）→ **3,304,638**（再合并同核小任务）。独立新评测目录复算3,304,638一致，1次实际新调用，耗时{replay['elapsed_seconds']:.1f}秒；这仅是该方案复评时间，不是整套搜索时间。

该图有17,995个计算算子，全部位于V计算通道，总工作7,714,975周期。旧方案全部放在一个Task里，实际完成时间只比这个固定方案的计算下界多548周期，所以继续调整其内部调度几乎没有空间；必须重新划分任务，才能利用多个核心。

原始数据中有11,004个32,768字节张量和7,016个2字节张量。新增通用方法 `p1_tensor_regions.py`：将大张量生产者/消费者合并到同一区域，将相连的低成本计算合并为短任务，再合并商图中的强连通分量以保证无环，最后按关键路径与最早完成时间分配核心。只使用张量大小、算子周期和依赖关系，没有图号特判，也没有修改输入。

在该图上，这形成了保留大张量计算链、在小归约结果之间切分的结构。首次优解为3965个Task；再以8个算子为新合并上限，合并同核相邻小任务，降至2440个Task。原有大于上限的Task单独保留。合并上限16的候选为3,820,239，劣于上限8，未采用。

代价也很明确：总COPY量由393,218增至119,950,386字节，新增119,557,168字节，spill仍为0。收益来自并行计算超过搬运和等待的代价，不能写成“比原单Task减少搬运”。先前逐层细切的必要DDR下界约1403万周期，本次最终方案约201万周期，说明应选择便宜的边界切分。

最终固定方案的Task下界为2,327,355，DDR下界为2,011,773，取最大值而非相加。它们只是当前划分/排程的必要条件，不能当作全局最优值或可达到的成绩。详见[结构与流量诊断](case016诊断.json)。

## 相同总调用上限的独立验证

每种方法从原图开始求解，五核P1、种子17/42/73、最多12次逻辑官方调用；没有历史最好方案作为起点。component用额度搜索整分量方案；adaptive搜索结构切分；hybrid预留4次额度，先做最多8次结构搜索，再对当前最佳方案作任务粒度/轨迹精修。前段未用满的额度可给精修，精修穷尽后的额度回到结构候选。三者总上限始终相同。

开发组沿用上一轮16图；扩展组在其补集中按相同四个结构层各抽2图，共8图：002、015、037、038、066、071、081、086。各图计算算子数不超过6000，选择不依赖本轮算法成绩。方法在扩展组评测前确定；扩展组未用于回调参数，但这些图曾参与历史全量搜索，不应称作完全未见数据。

按图取三种子耗时中位数，再配对比较：

| 组 | hybrid比较对象 | 胜 / 平 / 负 | 平均配对降时 |
|---|---|---:|---:|
{comparison_table}

合并24图，对component为13胜11平0负、平均配对降时8.14%；对adaptive为11胜12平1负、平均配对降时0.32%。这支持保留任务精修，但不支持“全面显著优于原切分”。case_048上adaptive为211,661，而hybrid为222,220，后者慢4.99%；固定预留额度使其没有尝试到一个有效的后续结构候选。

216个配置全部可行，逻辑调用1929次、实际新调用374次、超时0次；开发/扩展批次分别为87.1/112.7秒。**共享结果缓存，不能用这些秒数宣称冷启动提速；这是同调用上限，不是同墙钟预算。** 种子间有大量相同获胜方案，统计单位是24张图，不把72个图-种子组合当独立样本。逐图成绩和种子差异见同目录CSV。

张量区域算法是诊断大图后新增的定向探索，**没有加入上述hybrid对照**，case_016的57.17%收益也不计入上述24图胜率。

## 定向探索的成本与负例

| 批次 | 配置 | 官方调用 | 批次秒数 |
|---|---:|---:|---:|
{batch_table}

上述四批共19次调用，全部为新评测且成功；批次间有并发，不能相加当总耗时。case_003的三个张量区域候选分别为500657、504527、527356，均差于原409613；case_062的四个候选全部被必要下界排除，没有调用官方模拟器。保留原优解。完整尝试及剪枝原因见[候选表](张量分区候选.csv)、[剪枝表](张量分区剪枝.csv)。

## 全量方案与复用

全100图五核平均加速比，分母为每张图的原官方单核基准：

| 场景 | 第三轮 | 第四轮 |
|---|---:|---:|
{means}

这是累计不同搜索预算下的最好方案库；本轮P2/P3未新增搜索，沿用第三轮成果。完整1500覆盖与方法对照是两件事，不能把累计最好成绩当统一预算算法排名。

入口：[solve.py](../solve.py)，命令见[运行说明](../README.md)。从头模式新增 `--p1-method hybrid --seed 42`；已有方案可用 `--p1-refinement tensor`，再用 `--p1-refinement tasks --task-merge-caps 8,16` 接续。显式输入方案先官方复评，计入预算，适合换电脑运行。

下一步优先把张量区域候选纳入从头求解的统一总预算，并在更大图上比较；同时研究依据图结构分配“继续切分/任务精修”额度，处理case_048负例。大图突破尚不能直接推广到P2/P3缓存场景。

新方案与代码已保存，尚未Git提交。CSV中的原始官方记录路径指向本机历史目录；换电脑可直接用方案重新评测。

导出检查见[检查结果](检查结果.json)：1500个唯一配置的字段、核心数、成绩与官方成功记录对应，方案与被评测文件内容一致；14个目标值改善方案额外通过依赖图合法性检查。6项机制测试通过，便携两步入口实跑复现4,026,098→3,304,638。没有重复评测全1500份方案。
'''
    (out/'第四轮结果.md').write_text(text,encoding='utf-8')
    print(json.dumps(dict(report=str(out/'第四轮结果.md'),batches=batches),ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);main(p.parse_args().out)
