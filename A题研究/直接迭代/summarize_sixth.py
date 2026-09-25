"""Build sixth-round tables from completed experiments and the exported portfolio."""
import argparse,statistics
from collections import Counter
from common_run import *


def table(headers,rows):
    return '\n'.join(['| '+' | '.join(headers)+' |','|'+'|'.join('---' for _ in headers)+'|']+
                     ['| '+' | '.join(map(str,row))+' |' for row in rows])


def main(out):
    out=Path(out);runs=R/'直接迭代/运行结果';export=read_json(out/'summary.json')
    source=lambda name:read_json(runs/name/'summary.json')
    dev=set(read_json(runs/'缓存计划_v1/p3_selection.json')['cases'])
    ext=set(read_json(runs/'缓存计划_v1/p3_extension_selection.json')['cases'])
    methods={};costs=[];trials=[]
    names=[('P3读序开发_v1','read_order'),('P3原方法回归_v1','legacy'),
           ('P3读序扩展_v1','read_order'),('P3原方法扩展_v1','legacy'),('P3联合回归_v1','joint')]
    all_names=['综合新增大图_v1','综合冷启动_v1']+[n for n,_ in names]+[
        'P3读序归因_v1','P3读序归因扩展_v1','P3联合跨核_n2_v1','P3联合跨核_n3_v1','P3联合跨核_n4_v1',
        '推广缓存复用_v1','缓存独立复评_v1']
    for name in all_names:
        s=source(name);assert s['complete'] and s['completed']==s['expected']
        rows=s['rows'];costs.append(dict(batch=name,configs=len(rows),
            logical_calls=sum(r.get('logical_calls',1) for r in rows),
            new_calls=sum(r.get('new_calls',int(r.get('new_call',False))) for r in rows),wall_seconds=s['wall_seconds']))
    for name,method in names:
        for row in source(name)['rows']:
            methods[row['case'],method]=row
            s=read_json(runs/name/row['case']/'summary.json')
            for t in s['calls']:
                r=t['record'];meta=t.get('metadata',{})
                trials.append(dict(batch=name,case=row['case'],method=method,candidate=t['name'],
                    joint_stage=t.get('joint_stage'),mechanism=meta.get('mechanism'),status=r['status'],
                    makespan=score(r)[0] if r['status']=='success' else None,accepted=t['accepted'],
                    cache_hit=r['cache_hit'],record=r['record_path']))
    compare=[]
    for case in sorted(dev|ext):
        row=dict(case=case,group='development' if case in dev else 'extension',before=methods[case,'legacy']['before'])
        for method in ('legacy','read_order','joint'):
            for field in ('after','logical_calls','new_calls'):row[method+'_'+field]=methods[case,method][field]
        compare.append(row)
    comparisons=[]
    for group,selected in [('development',dev),('extension',ext),('all',dev|ext)]:
        for method,control in [('read_order','legacy'),('joint','legacy'),('joint','read_order')]:
            pairs=[(methods[c,method]['after'],methods[c,control]['after']) for c in sorted(selected)]
            comparisons.append(dict(group=group,method=method,control=control,cases=len(pairs),
                wins=sum(a<b for a,b in pairs),ties=sum(a==b for a,b in pairs),losses=sum(a>b for a,b in pairs),
                mean_paired_reduction_pct=statistics.mean(100*(1-a/b) for a,b in pairs)))
    attribution=source('P3读序归因_v1')['rows']+source('P3读序归因扩展_v1')['rows']
    assert all(r['all_success'] for r in attribution)
    p1=source('综合新增大图_v1')['rows'];p1map={(r['case'],r['method']):r for r in p1}
    p1table=[]
    for case in sorted({r['case'] for r in p1}):
        a=p1map[case,'hybrid']['makespan'];b=p1map[case,'portfolio_diverse']['makespan']
        p1table.append([case,a,b,f'{100*(1-b/a):.2f}%'])
    cold=source('综合冷启动_v1')['rows'];cli=source('综合冷启动命令_v1')
    cold.append(dict(case=cli['case'],method=cli['variant'],makespan=score(cli['best_record'])[0],
        logical_calls=cli['logical_calls'],new_calls=cli['new_calls'],elapsed_seconds=cli['elapsed_seconds'],
        timeouts=sum(t['record']['status']=='timeout' for t in cli['evaluations']),stop_reason=cli['stop_reason']))
    diagnose=source('缓存诊断_v1')['rows']
    diagnosis=dict(graphs=len(diagnose),graphs_with_concurrent_misses=sum(r['concurrent_misses']>0 for r in diagnose),
        graphs_with_post_eviction_misses=sum(r['post_eviction_misses']>0 for r in diagnose),
        graphs_without_repeated_reads=sum(r['repeated_tensors']==0 for r in diagnose),
        scope='100 current five-core P3 plans; no new official calls; not all possible plans')
    write_csv(out/'P3方法比较.csv',compare);write_csv(out/'P3比较汇总.csv',comparisons)
    write_csv(out/'P3机制归因.csv',attribution);write_csv(out/'P3候选记录.csv',trials)
    write_csv(out/'P1新增大图.csv',p1);write_csv(out/'P1完整冷启动.csv',cold)
    write_csv(out/'批次成本.csv',costs);write_csv(out/'P3全100图诊断.csv',diagnose)
    atomic_json(out/'P3诊断摘要.json',diagnosis);atomic_json(out/'独立复评.json',source('缓存独立复评_v1'))
    atomic_json(out/'实验选择.json',{name:read_json(runs/'缓存计划_v1'/name) for name in ['large_selection.json','p3_selection.json','p3_extension_selection.json']})
    score_rows=list(csv.DictReader((out/'改善清单.csv').open(encoding='utf-8-sig')))
    counts=Counter(int(r['problem']) for r in score_rows if int(r['after'])<int(r['before']))
    main_rows=[r for r in score_rows if int(r['cores'])==5 and r['case'] in ('case_085','case_075','case_043','case_094','case_090','case_052')]
    ctable=table(['图','场景','上轮周期','本轮周期','降低'],[[r['case'],'P'+r['problem'],f"{int(r['before']):,}",f"{int(r['after']):,}",f"{float(r['reduction_pct']):.2f}%"] for r in main_rows])
    comp_table=table(['范围','方法 / 对照','胜 / 平 / 负','平均配对降时'],[
        [r['group'],r['method']+' / '+r['control'],f"{r['wins']} / {r['ties']} / {r['losses']}",f"{r['mean_paired_reduction_pct']:.2f}%"]
        for r in comparisons if r['control']=='legacy'])
    mechanism_table=table(['图','P2控制→读序','P3控制→读序','额外缓存交互收益'],[
        [r['case'],f"{r['control_p2']} → {r['guided_p2']}",f"{r['control_p3']} → {r['guided_p3']}",r['cache_interaction']]
        for r in sorted(attribution,key=lambda x:x['case']) if r['case'] in ('case_090','case_078','case_052','case_017')])
    coldtable=table(['图','周期','调用 / 新调用','完整搜索秒数','超时次数','停止原因'],[
        [r['case'],r['makespan'],f"{r['logical_calls']} / {r['new_calls']}",f"{r['elapsed_seconds']:.1f}",r['timeouts'],r['stop_reason']] for r in cold])
    note=f'''# 第六轮：P3关键读取顺序与缓存归因，P1大图和冷启动验证

2026年9月25日。本轮已结束。相对第五轮，**{export['strict_time_improved']}个配置进一步降时，{export['objective_improved']-export['strict_time_improved']}个同耗时次指标改善，保留方案零回退**。其中P1/P2/P3降时配置数为{counts[1]}/{counts[2]}/{counts[3]}。全部1500份官方可行方案已导出。论文暂缓，旧长队列保持暂停。

## 新成绩

以下为五核配置；周期是原官方模拟器结果，不是Python运行秒数。

{ctable}

所有改善见[清单](改善清单.csv)，包括定向跨核实验。P2改善来自把P3搜索得到的方案交给原版P2评测后择优；没有修改P2/P3硬件参数。三种P3方法和追加搜索的累积最好结果不能作为一个统一预算方法的成绩。

## 先诊断，再选图

读取第五轮100张图的五核P3完整时间线，并逐条验证COPY、缓存事件和FIFO状态。{diagnosis['graphs_with_concurrent_misses']}图有首次读完成前的重复miss，{diagnosis['graphs_with_post_eviction_misses']}图有淘汰后再次miss，{diagnosis['graphs_without_repeated_reads']}图没有重复读。此前早期108组未见淘汰后miss的结论仅适用于当时方案，不能沿用到当前方案库。

根据已观测读延迟、后续计算剩余工作及消费者等待间隔形成启发式优先级。先选相对优先级最高的6图，再选2张有淘汰后miss的图，加001/008两张低潜力对照；随后保持读序生成器不变，验证排序靠后的另外8图。选择在新成绩产生前写入文件，见[实验选择](实验选择.json)。这是有针对性的方案开发与扩展验证，既不是随机总体估计，也不是完全未见图测试。

## 方法实现及同上限比较

`p3_read_order.py`先生成观测顺序的单算子子图控制方案，全部读序候选与该控制保持**完全相同的node_to_subgraph和算子分核**，仅改变合法core_schedules。根据关键读，尝试提前消费者及必要祖先、提前其他核已有首读者，或把现有独立工作移到跟随读取之前；以8/32/128个局部算子为窗口，交替覆盖机制与尺度。不插入假操作、等待、预取，不更改评测器。最终只按官方周期及搬运次指标接受。

旧方法legacy还允许少量消费者迁核，因而与固定分核读序方法互补。新增joint先分配至多4次给旧邻域，再把余下额度用于读序精修，后段从前段最好方案出发。**总上限仍为12次**，不是两套各跑12次。三个方法使用相同第五轮起点，P2方案继承在比较中关闭，缓存命中也占逻辑额度，每图120秒软预算。候选不足允许提前结束。

{comp_table}

joint是在看到两种方法结果后设计的，因此其18图结果均属于开发回归。新方法没有全面支配旧方法，默认入口仍保留legacy；也没有用最终方案库的跨方法择优冒充某一种方法成绩。逐图值和实际调用数见[P3方法比较](P3方法比较.csv)，全部候选含负例见[候选记录](P3候选记录.csv)。这批使用固定种子17，未声称完成多种子稳定性研究。

## 缓存到底贡献了什么

对18张图的原始方案、编码控制及读序候选，分别在P2/P3下官方复评。16图有合法不同的读序候选；001/008只保留控制，不凭空制造缓存候选。候选先按P3成绩选出，以下是选定方案的配对归因，不是独立随机消融。

定义排程收益D2=T2(control)-T2(guided)，P3收益D3=T3(control)-T3(guided)，缓存交互收益I=D3-D2。I也等于新旧方案各自P2-P3差值的变化，表示缓存与排程的联合作用；它不是某一条COPY节省时间的简单求和。

{mechanism_table}

090和078在P2下变慢、P3下变快，提供缓存相关改善的直接证据。052在P2/P3均节省1935周期，主要是排程收益，不能称为额外缓存收益。017命中率由控制的约11.57%升至27.17%，总周期仍为32681；命中率不能当最终目标。完整数值及其他正负例见[机制归因](P3机制归因.csv)。控制重编码本身也可能改变排程，因此须同时与原始方案比较；例如070相对控制大幅改善，但比原始最好方案只快1周期。

## P1新增大图与完整冷启动

从未参与第五轮小图及四图路线比较的其余大图中，按重分量/分散分量各抽2图，固定抽样种子20260928，未依据本批成绩修改求解规则。每法12次调用、单次60秒、总180秒上限，种子17：

{table(['图','hybrid','portfolio_diverse','配对降低'],p1table)}

4图全部找到可行解、无超时；新方法2胜2平。085上两法成绩相同，虽都改善历史最好方案，也不能算新方法对hybrid的胜出。图在历史研究中曾出现，不能声称完全未见验证。

此前运行时间包含成功缓存复用；本轮新增`--fresh-evaluations`，在全新目录完整执行候选生成、搜索与官方评测：

{coldtable}

上述时间是本机本次负载下的墙钟观测；075/085并发，043通过命令行单独验证，并非严格隔离机器负载的性能基准。只覆盖这3图，不能推广为所有图均在相同时间内求完，也未宣称全局最优。全部实际调用及是否超时已保留。

## 交付及下一步

最新[方案目录](方案)、[全部成绩](全部成绩.csv)、[检查结果](检查结果.json)可直接使用。P1/P2/P3五核100图平均加速比分别为{next(g['after_mean_speedup'] for g in export['groups'] if g['problem']==1 and g['cores']==5):.4f}、{next(g['after_mean_speedup'] for g in export['groups'] if g['problem']==2 and g['cores']==5):.4f}、{next(g['after_mean_speedup'] for g in export['groups'] if g['problem']==3 and g['cores']==5):.4f}，均相对各图原官方单核基准；它们属于累计方案库成绩。

三份P3代表方案及一份P2方案另在空评测目录独立复算，[记录](独立复评.json)。成本表[批次成本](批次成本.csv)列实际批次，含缓存和并发，不能简单相加当冷启动时间。逐条官方记录路径仍指向本机历史目录；换电脑可用入库方案复评继续。

新入口见[运行说明](../README.md)。后续优先补齐三方法更多图的同预算比较，研究如何按图特征分配迁核和读序预算；保留当前负例，避免只围绕赢家调参。论文仍按用户要求暂缓。本轮代码和成果尚未Git提交。
'''
    (out/'第六轮结果.md').write_text(note,encoding='utf-8')
    print(json.dumps(dict(exported=export['coverage'],strict=export['strict_time_improved'],comparisons=comparisons,cold=cold),ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);a=p.parse_args();main(a.out)
