"""Fifth-round artifacts: cumulative plans, bounded comparisons and failures."""
import argparse,shutil
from common_run import *


def main(out):
    out=Path(out);runs=R/'直接迭代/运行结果';portfolio=read_json(out/'summary.json')
    small=read_json(runs/'综合小图最终比较_v1/analysis.json');large=read_json(runs/'综合大图最终比较_v1/analysis.json')
    replay=read_json(runs/'综合独立复评_v1/summary.json')
    assert replay['complete'] and all(r['matches'] and r['new_call'] for r in replay['rows'])
    for folder,label in [('综合小图最终比较_v1','小图'),('综合大图最终比较_v1','大图')]:
        for src,dst in [('按图比较.csv','逐图比较.csv'),('种子差异.csv','种子差异.csv'),('results.csv','全部配置.csv'),('analysis.json','比较汇总.json')]:
            shutil.copyfile(runs/folder/src,out/(label+dst))
    batches=[];attempts=[];routing=[]
    names=['综合回归_v1','综合粒度回归_v1','综合大图_v1','综合粒度大图_v1','综合开发诊断_v1',
           '综合张量跨核_v1','综合跨核合并_v1','综合核数推广_v1']
    for name in names:
        s=read_json(runs/name/'summary.json');assert s['complete']
        rows=s['rows'];batches.append(dict(batch=name,configs=len(rows),logical_calls=sum(r['logical_calls'] for r in rows),
            fresh_calls=sum(r['new_calls'] for r in rows),wall_seconds=s['wall_seconds'],
            no_feasible=sum('makespan' in r and r['makespan'] is None for r in rows)))
        files=list((runs/name).glob('case_*/*/seed*/summary.json'))+list((runs/name).glob('slots/*/*/summary.json'))
        for path in files:
            slot=read_json(path)
            for trial in slot['evaluations']:
                r=trial['record'];attempts.append(dict(batch=name,case=slot['case'],cores=slot['num_cores'],
                    method=slot.get('variant','targeted_warm'),candidate=trial['name'],phase=trial.get('phase','warm'),
                    status=r['status'],makespan=r['metrics'].get('makespan'),cache_hit=r['cache_hit'],accepted=trial['accepted'],
                    official_record=r['record_path']))
            routing.extend(dict(batch=name,case=slot['case'],method=slot.get('variant'),**e) for e in slot.get('routing',[]))
    write_csv(out/'批次运行成本.csv',batches);write_csv(out/'候选评测记录.csv',attempts);write_csv(out/'候选路线记录.csv',routing)
    atomic_json(out/'独立复评.json',dict(rows=[{k:v for k,v in r.items() if k!='record'} for r in replay['rows']],
        records=[r['record']['record_path'] for r in replay['rows']],scope='three fresh individual evaluations, not full cold algorithm benchmark'))
    with (out/'全部成绩.csv').open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
    examples='\n'.join(f"| {r['case']} | {r['cores']} | {int(r['before']):,} | {int(r['after']):,} | {float(r['reduction_pct']):.2f}% |"
                       for r in rows if r['problem']=='1' and r['cores']=='5' and r['case'] in ('case_002','case_024','case_053','case_063'))
    small_table='\n'.join(f"| {m} | {c['wins']} / {c['ties']} / {c['losses']} | {c['mean_paired_time_reduction_pct']:.2f}% |" for m,c in small['comparisons'].items())
    with (out/'大图逐图比较.csv').open(encoding='utf-8-sig') as f:bigrows=list(csv.DictReader(f))
    big_table='\n'.join(f"| {r['case']} | {r['adaptive'] or '无可行解'} | {r['hybrid'] or '无可行解'} | {r['portfolio'] or '无可行解'} | {r['portfolio_diverse']} |" for r in bigrows)
    cost_table='\n'.join(f"| {b['batch']} | {b['configs']} | {b['logical_calls']} / {b['fresh_calls']} | {b['wall_seconds']:.1f} | {b['no_feasible']} |" for b in batches)
    means='\n'.join(f"| P{g['problem']} | {g['before_mean_speedup']:.4f} | {g['after_mean_speedup']:.4f} |" for g in portfolio['groups'] if g['cores']==5)
    text=f'''# 第五轮：统一求解流程与限时可行性

2026年9月25日。本轮已结束。相对第四轮结束时，**{portfolio['strict_time_improved']}个配置进一步降时，另{portfolio['objective_improved']-portfolio['strict_time_improved']}个同耗时减少搬运，保留方案零回退**；1500/1500份方案已导出。P2/P3本轮未新增搜索。论文仍暂缓，旧长队列仍暂停。

## 最新成果

| 图 | 核数 | 第四轮周期 | 第五轮周期 | 降低 |
|---|---:|---:|---:|---:|
{examples}

case_024的2/3/4/5核分别达到1,603,810、1,315,118、1,214,514、1,095,114周期，原先均为2,555,343。case_002的3、4核，以及case_063的2、3核也有改善。case_048本轮仅有同耗时次指标改善，不能把对照实验中的恢复重复计成对历史最好成绩的新降时。

已独立新评测case_002五核78,244和case_024五核1,095,114，均一致。方案在[方案目录](方案)，完整成绩及来源在[全部成绩](全部成绩.csv)，增量在[改善清单](改善清单.csv)。周期数是官方模拟耗时，与Python运行秒数不同。

## 新的通用求解流程

新增 `p1_portfolio.py`，在一个总调用/时间预算内串联结构切分、张量区域候选和任务精修。`portfolio`为初版，`portfolio_diverse`为本轮修订版；原adaptive/hybrid仍可显式选择，默认入口未替换。

1. 在12次总调用中，前段最多用8次评估强分量基线和结构切分。使用必要Task及DDR下界取最大值剪枝，绝不把二者相加。
2. 如果张量候选的代理时间不超过当前官方最好周期的85%，尝试最多2个。**85%是启发式预算门槛，不是安全下界，也没有被证明最优。** 真正的必要下界另外检查。
3. 若当前只有一个Task，跳过任务层重排/合并，将剩余额度留给结构切分；若有至少4N个Task且算子数中位数不超过8，先尝试8/16上限合并，再尝试普通任务邻域。
4. 修订版对重分量图按“切分方法×粒度”轮换，先覆盖不同粒度，再试同粒度的更多排程变体；对超过6000个计算算子的大图，基线按最大Task大小从小到大尝试，争取先得到可行解。

候选只由图结构、本次已评测方案及官方时间线产生，没有图号特判、没有历史最好方案热启动。结果缓存命中仍计入逻辑调用；追加精修与跨核推广单独记录。

## 24图回归比较

沿用前两轮24张中小图，三种子17/42/73、五核、每种方法最多12次调用、单次最多60秒。总时间上限未实际约束小图。每张图先取三种子中位数再比较，最终修订版结果如下：

| 对照方法 | 胜 / 平 / 负 | 平均配对降时 |
|---|---:|---:|
{small_table}

case_048原先hybrid因提前花掉精修额度得到222,220，adaptive为211,661。初版portfolio虽跳过单Task精修，却被过于乐观的张量代理占掉2次调用，仍为222,220。粒度轮换把有效切分提前，修订版恢复211,661。这个修正利用了该图的失败经验，因此这里是开发回归结果，不能称为独立未见测试集。

修订版相对初版为1胜23平0负；相对hybrid仍有case_086负例：95,806对95,481，慢约0.34%。case_002、024是主要收益来源，不能把平均改善解读为每张图都提升。

最终比较由两个已完成批次按完整方法组汇总为288条配置成绩，没有跨运行挑选最好种子；实际方案的累计择优另在1500份方案库中进行。大多数图的三个种子给出相同方案，统计单位仍是24张图。详见[逐图比较](小图逐图比较.csv)、[种子差异](小图种子差异.csv)。

## 大图的限时问题

从计算算子超过6000的图中，按“重分量/分散分量”各抽2张，随机种子20260927；排除用于张量方法开发的003、016、062，得到014、047、053、063。其他历史实验仍用过这些图，因此不称为完全未见数据。本组仅种子17，每方法最多12次调用、单次60秒、每图180秒软预算。

| 图 | adaptive | hybrid | portfolio初版 | portfolio_diverse |
|---|---:|---:|---:|---:|
{big_table}

014旧候选顺序先评巨大Task，3次超时耗尽窗口，没有可行解；修订版先得到3,859,241，再在本批达到3,641,147，与已有历史最好成绩相同。修订版仍有3个超时候选，总搜索触及180秒，因此不能写成“消除了超时”或“完成全部候选”。

修订版4/4图有可行解，其他三个方法均3/4。对adaptive的3个有效配对为2胜1平；对hybrid为2胜1负，047上325,792差于hybrid的323,254。失败配置不记作零周期或无限加速，不参与速度均值；可行性单列。大图只有4张、1个种子，且设有墙钟截断，结论范围有限。

**缓存限制：**完整比较复用了官方成功缓存，不是冷启动性能评测。为核对014的可行性入口，首个候选单独放入新的空评测目录重算，约14秒成功、周期一致；这证明该候选本身能较快评测，不能据此声称整套冷启动搜索达到本表时间或分数。数据见[独立复评](独立复评.json)、[大图全部配置](大图全部配置.csv)。

016开发诊断也已在统一12次上限中复现3,304,638，修订版单图入口实跑10次调用取得同值；它属于方法开发图，单列而不混入新抽样大图胜率。

## 实际运行成本与完整交付

| 实际批次 | 配置 | 逻辑调用 / 新调用 | 批次秒数 | 无可行结果配置 |
|---|---:|---:|---:|---:|
{cost_table}

批次有并发和缓存复用，不将秒数相加冒充冷启动总时长，也不把派生比较表算作新的实验。全部负例和超时保存在[候选记录](候选评测记录.csv)，分流判断在[路线记录](候选路线记录.csv)。跨核推广是在五核成功后定向追加的预算，不计入统一预算排名。

全100图五核平均加速比（逐图相对原官方单核基准取比值，再算术平均）：

| 场景 | 第四轮 | 第五轮 |
|---|---:|---:|
{means}

这是累积方案库成绩，不是统一预算算法成绩。当前全部1500份方案均有官方成功记录，覆盖不代表最优。最新统一入口及可复现命令见[运行说明](../README.md)，使用 `--p1-method portfolio_diverse --budget 12 --seconds 180 --evaluation-timeout 60` 从原图求解，支持1—5核。

下一步应将本轮确定的流程用于更多尚未参与路线调参的大图，扩大证据范围，并给P3缓存关键读路径补上独立机制实验；不能用P1的收益代替P3证据。论文写作仍按用户要求暂缓。

新改动尚未Git提交。CSV中的原始官方记录路径指向本机历史目录，其他电脑可直接用方案官方复评。导出和机制检查记录在[检查结果](检查结果.json)。
'''
    (out/'第五轮结果.md').write_text(text,encoding='utf-8')
    print(json.dumps(dict(report=str(out/'第五轮结果.md'),strict_time_improved=portfolio['strict_time_improved'],batches=len(batches)),ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);main(p.parse_args().out)
