"""Human-readable second-round report from exported scores and batch receipts."""
import argparse
from common_run import *

def main(a):
    out=Path(a.out).resolve();s=read_json(out/'summary.json')
    with (out/'全部成绩.csv').open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
    lines=['# 第二轮结果：推广到全部核数','',
      f'方案覆盖 {s["coverage"]}/1500，本轮新增覆盖 {s["new_coverage"]} 个单核配置。相对本轮开始记录，严格降时 {s["strict_time_improved"]} 个配置，另有 {s["objective_improved"]-s["strict_time_improved"]} 个耗时持平、复制量减少；保留方案的耗时回退 {s["regressions"]} 个。','',
      '覆盖齐全表示每个配置都有官方成功记录，不代表全局最优或研究完成。初始方案生成、跨配置复用和追加精修使用了不同预算，以下是累计最好方案库成绩。','',
      '## 100图逐图平均加速比','', '| 场景 | 核数 | 本轮前 | 本轮后 | 耗时改善图数 | 平均逐图降时 |','|---|---:|---:|---:|---:|---:|']
    for g in s['groups']:
        lines.append(f'| P{g["problem"]} | {g["cores"]} | {g["before_mean_speedup"]:.6f} | {g["after_mean_speedup"]:.6f} | {g["strict_time_improved"]}/100 | {g["mean_paired_time_reduction_pct"]:.3f}% |')
    lines+=['','加速比为原官方整图单核时间除以该配置官方耗时，逐图求比再平均。单核补齐另列，不混入前后完整分组比较；P1/P2题面主曲线的1核参考点仍为1。实际单核方案耗时完整保存在全部成绩.csv中。','',
      '## 本轮新增的代表性收益','', '| 配置 | 本轮前官方耗时 | 本轮后官方耗时 | 降时 |','|---|---:|---:|---:|']
    for p in (1,2,3):
        rr=[r for r in rows if int(r['problem'])==p and r['before'] and float(r['after'])<float(r['before'])]
        for r in sorted(rr,key=lambda r:float(r['after'])/float(r['before']))[:3]:
            lines.append(f'| P{p} / {r["case"]} / {r["cores"]}核 | {r["before"]} | {r["after"]} | {float(r["reduction_pct"]):.2f}% |')
    lines+=['','这些数值是官方模拟耗时，不是电脑上的运行秒数。比较分母来自本轮启动时保存的已有最好记录，包含上一轮成果。','',
      '## 批次与实际预算','', '| 批次 | 已返回/原计划 | 逻辑调用 | 新评测 | 观察墙时 | 状态 |','|---|---:|---:|---:|---:|---|']
    root=R/'直接迭代/运行结果'
    for d in sorted(root.glob('推广*')):
        f=d/'summary.json'
        if not f.exists():continue
        z=read_json(f);rr=z.get('rows',[])
        state='调整排期，结果保留' if z.get('interrupted') else '批次结束' if z.get('complete') else '部分结果'
        lines.append(f'| {d.name} | {len(rr)}/{z.get("expected",len(rr))} | {sum(r.get("logical_calls",0) or 0 for r in rr)} | {sum(r.get("new_calls",0) or 0 for r in rr)} | {z.get("wall_seconds",0):.2f}s | {state} |')
    lines+=['','批次曾并发运行，墙时不能相加当总等待时间；命中历史成功缓存也计入逻辑调用。已有方案作为追加精修的起点，不能当作免费的从头求解。','',
      'P1最初先处理大图，每配置60秒。观察到开销集中后，保留已返回配置，其余配置改为先小图、每配置最多4次评测与20秒。P3同样采用每配置最多4次评测与20秒。时间在候选边界检查，候选生成和IO可能小幅超出；触及时间上限不代表全部候选已搜索完。','',
      '## P2轨迹精修的边际收益','']
    pf=root/'推广P2轨迹_v1/summary.json'
    if pf.exists():
        pr=read_json(pf);pw=[r for r in pr['rows'] if r['after']<r['before']]
        lines.append(f'18张图、2—4核共 {len(pr["rows"])} 个配置，每配置最多4次追加评测与15秒。观察墙时 {pr["wall_seconds"]:.2f} 秒，{len(pw)} 个配置降时，最大追加降时 {max((r["reduction_pct"] for r in pw),default=0):.3f}%。面板包含此前固定开发组和互用后值得检查的配置，不能当全100图随机样本。此次收益较小，未继续扩大该阶段预算。')
    lines+=['','## P1大图与单核补齐','',
      '官方P1在事件循环中会为当前活动Task重新建立算子列表并检查完成状态，超大Task带来重复扫描。我们未修改官方代码，而是为缺失单核构造最多1024个原始计算算子的Task：小的独立WCC可合并，过大的WCC按拓扑序连续切分。分块可能改变任务等待、COPY和最终耗时，因此每份方案必须再由官方评分，不能推算成绩。','',
      '全部100图的这类分块方案已通过结构检查，包括原算子完整覆盖、收缩依赖无环、核内顺序合法和Task大小上限。实际需要补齐的分块配置均单独官方评测；早期整体任务超时记录保留。','',
      'case014整图单Task尝试在90秒内超时，分块版本约23秒完成；不同方案和并发状态下的观察值不能换算成同算法冷启动提速倍数。分块填补输出缺口，不构成“单核最优”的证明。','',
      '## P3收益归因','']
    records=[read_json(p) for p in (root/'推广P3精修_v1').glob('slots/case_*/p*_n*/summary.json')]
    control_slots=guided_slots=0
    for r in records:
        phases=[x for x in r.get('phases',[]) if 'round' in x]
        control_slots+=any(x['after_control']<x['before'] for x in phases)
        guided_slots+=any(x['after_guided']<x['after_control'] for x in phases)
    lines+=[f'P3精修阶段已返回 {len(records)} 个配置，其中 {control_slots} 个出现调度控制降时，{guided_slots} 个出现缓存引导候选在控制之后继续降时；两类配置可重叠。这里的“缓存引导”是候选生成方式，可能改变调度，不能直接等同于纯硬件缓存贡献。',
      'P2/P3方案互用与随后精修分别记录；当前累计最优方案库不作为旧四格硬件对照的选定方案，上一轮固定计划的机制对照仍独立保留。']
    reverse=root/'推广互用_v2/summary.json'
    if reverse.exists():
        rr=read_json(reverse)['rows'];p2wins=sum(r['problem']==2 and r['after']<r['before'] for r in rr)
        lines.append(f'第二次互用检查中，{p2wins} 个P2配置采用P3选中的方案后降时。相同P2硬件场景中也能获益，表明这些改进至少部分来自方案/调度变化，不能全部写成缓存硬件收益。')
    lines+=['',
      '## 直接使用与换电脑','',
      '- `方案/p*/n*/case_XXX_multicore_res.json`：官方两字段方案。',
      '- `全部成绩.csv`、`改善清单.csv`、`补齐清单.csv`、`各核数汇总.csv`：完整结果与本轮变化。',
      '- `../solve.py --cores N --incumbent-plan 路径`：显式加载一份方案，先官方复评，再做短预算精修；不查询原电脑历史目录。初始复评计入预算。',
      '- P1三核、P2三核和P3二核的显式方案入口已实际运行；此前的单核边界和全100图结构检查也已完成。',
      '- CSV中的历史官方记录绝对路径仍指向原电脑；精选方案本身独立可用，不需要这些路径。','',
      '## 尚需推进','',
      '继续用强Component基线、统一预算和有针对性的多种子实验验证方法优势；针对大图和收益较弱的配置改进操作分配及关键读调度。当前完整输出不是最优性证明，也不足以单独支持获奖结论。论文写作按此前要求暂不启动。']
    (out/'第二轮结果.md').write_text('\n'.join(lines)+'\n',encoding='utf-8');print(out/'第二轮结果.md')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);main(p.parse_args())
