"""Rebuild round-12 evidence tables and the result note from completed records."""
from collections import defaultdict
from pathlib import Path
import csv
import shutil
from common_run import read_json, atomic_json, write_csv


HERE = Path(__file__).resolve().parent
RUNS = HERE/'运行结果'
OUT = HERE/'第十二轮成果'
PANELS = {
    '开发v1': '端到端第十二轮开发_v1',
    '开发v2': '端到端第十二轮开发_v2',
    '开发v3': '端到端第十二轮开发_v3',
    '验证v3': '端到端第十二轮验证_v3',
    '预算24v3': '端到端第十二轮预算24_v3',
}


def table(headers, rows):
    return '\n'.join(['|'+'|'.join(headers)+'|', '|'+'|'.join(['---']*len(headers))+'|']+
                     ['|'+'|'.join(map(str, r))+'|' for r in rows])


def main():
    export = read_json(OUT/'summary.json')
    check = read_json(OUT/'检查结果.json')
    replay = read_json(RUNS/'端到端第十二轮独立复评_v1/summary.json')
    portable = read_json(RUNS/'端到端第十二轮隔离复现_v2/summary.json')
    cross = read_json(RUNS/'推广第十二轮跨场景_v1/summary.json')
    assert export['coverage'] == check['dag_validated_plans'] == 1500
    assert replay['complete'] and all(r['matches'] and r['new_call'] for r in replay['rows'])
    assert portable['complete'] and all(r['matches'] and r['fresh'] == 12 for r in portable['runs'])
    with (OUT/'改善清单.csv').open(encoding='utf-8-sig') as f:
        improved = list(csv.DictReader(f))
    assert len(improved) == len(replay['rows']) == 7 and export['regressions'] == 0
    panels = {}; costs = []; all_comparisons = []
    for label, name in PANELS.items():
        folder = RUNS/name
        summary = read_json(folder/'summary.json'); analysis = read_json(folder/'analysis.json')
        assert summary['complete'] and summary['completed'] == summary['expected']
        rows = summary['rows']; panels[label] = (summary, analysis)
        costs.append(dict(panel=label, runs=len(rows), logical_calls=sum(r['logical_calls'] for r in rows),
            fresh_calls=sum(r['new_calls'] for r in rows), failed_calls=sum(r['failed_calls'] for r in rows),
            generation_errors=sum(r['generation_errors'] for r in rows), wall_seconds=summary['wall_seconds']))
        all_comparisons.extend(dict(panel=label, **r) for r in analysis['comparisons'])
        target = OUT/'实验对照'/label; target.mkdir(parents=True, exist_ok=True)
        for filename in ('plan.json', 'summary.json', 'analysis.json', 'results.csv', 'paired.csv', 'comparisons.csv', 'costs.csv', 'curves.csv'):
            shutil.copyfile(folder/filename, target/filename)
    write_csv(OUT/'实验成本.csv', costs)
    write_csv(OUT/'方法对照.csv', all_comparisons)
    atomic_json(OUT/'独立复评.json', replay)
    atomic_json(OUT/'隔离复现.json', portable)
    atomic_json(OUT/'源码依赖清单.json', read_json(RUNS/'端到端第十二轮隔离复现_v2/inputs.json'))
    atomic_json(OUT/'跨场景补测.json', cross)
    families = defaultdict(lambda: dict(evaluations=0, incumbent_updates=0))
    generation = defaultdict(float)
    for path in (RUNS/PANELS['验证v3']).glob('slots/case_*/p*_n*/*/summary.json'):
        if path.parent.name == 'baseline': continue
        s = read_json(path); method = path.parent.name
        for c in s['calls']:
            if c['stage'] == 'continuation':
                row = families[s['problem'], method, c['phase']]
                row['evaluations'] += 1; row['incumbent_updates'] += int(c['accepted'])
        for stage in s['stages']:
            if 'seconds' in stage:
                generation[s['problem'], method, stage.get('family', stage['phase'])] += stage['seconds']
    family_rows = [dict(problem=p, method=m, family=f, **v) for (p,m,f),v in sorted(families.items())]
    write_csv(OUT/'验证组邻域调用.csv', family_rows)
    write_csv(OUT/'验证组候选生成耗时.csv', [dict(problem=p, method=m, family=f, seconds=v) for (p,m,f),v in sorted(generation.items())])
    validation = panels['验证v3'][1]
    main_rows = []
    for p in (1,2,3):
        c = next(r for r in validation['comparisons'] if r['problem']==p and r['method']=='integrated' and r['control']=='baseline')
        times = {r['method']: r['median_run_seconds'] for r in validation['costs'] if r['problem']==p}
        main_rows.append([f'P{p}', c['pairs'], f"{c['wins']}/{c['ties']}/{c['losses']}",
            f"{c['mean_paired_reduction_pct']:.4f}%", f"{c['median_paired_reduction_pct']:.4f}%",
            f"{times['baseline']:.2f} → {times['integrated']:.2f}"])
    ablation_rows = []
    for c in validation['comparisons']:
        if c['method']=='integrated' and c['control']=='local_legacy':
            ablation_rows.append([f"P{c['problem']}", f"{c['wins']}/{c['ties']}/{c['losses']}", f"{c['mean_paired_reduction_pct']:.4f}%"])
    gain_rows = []
    for r in improved:
        source = next(m for m in ('integrated','local_legacy','baseline') if m in Path(r['official_record']).parts)
        gain_rows.append([r['case'][-3:], 'P'+r['problem'], r['before'], r['after'], f"{float(r['reduction_pct']):.4f}%", source])
    total_fresh = sum(r['fresh_calls'] for r in costs)+sum(r['fresh'] for r in portable['runs'])+cross['this_invocation_new_calls']+len(replay['rows'])
    total_logical = sum(r['logical_calls'] for r in costs)+sum(r['calls'] for r in portable['runs'])+cross['this_invocation_logical_calls']+len(replay['rows'])
    accounting = dict(panel_runs=sum(r['runs'] for r in costs), panel_fresh_calls=sum(r['fresh_calls'] for r in costs),
        all_logical_calls=total_logical, all_fresh_calls=total_fresh, export_changed=7,
        scope='includes development revisions, validation, budget probes, isolated CLI runs, cross-scene probes and independent replay; not an equal-budget algorithm score')
    atomic_json(OUT/'本轮执行汇总.json', accounting)
    note = f'''# 第十二轮结果：把局部改进接入从原图开始的统一预算求解器

本轮交付完整1500份方案，相对第十一轮新增7个严格降时配置：P1为0、P2为4、P3为3，无退步。全部7项已在全新目录独立调用官方评测器，周期和COPY字节均完全一致；1500份方案全部通过依赖合法性、核心数量及评测输入顺序一致性检查。

算法方面，P1在本轮预留的4张五核验证图上，用12次评测获得平均16.57%的配对降时。P2/P3同预算的平均收益很小，新增P3邻域相比同框架旧邻域没有最终收益。因此保留现有默认方法，允许显式选择integrated，不统一替换。

## 实际实现

cold_portfolio.py现从原始计算图出发，前段最多6次调用建立本次起点，剩余预算统一安排结构候选与局部精修。P1接入联合Task拆分、分核；P2/P3接入区域调整、输入复用和数据存活时间优化，并保留旧轨迹/缓存邻域。每次改善后，下一次局部生成读取新的官方轨迹。

不读取历史最好方案；初次评测、失败和缓存命中都计费，所有阶段共用时间上限。P1仅用必要下界剪枝。P2/P3的35%数据压力阈值、WCC的90%计算代理门槛只是调度启发式。WCC额外探测和完整分量布局后的配套探测都占用原预算。

组件布局即使暂时输给当前最佳方案，也可能经过WCC交织后反超，因此保留独立的完整分量起点。这是本轮修复早期退步的关键。具体开发版本、参数与样本在上级目录《第十二轮实验方案.md》中。

## 本轮预留验证：相同12次评测

均为五核、seed17、每臂90秒软上限、单次25秒上限，全新独立评测目录。P1基线是portfolio_diverse，P2/P3是当前默认trace_routed。平均降时为逐图计算100×(1−新周期/基线周期)后取算术平均，不是累计最好方案提升。

{table(['场景','图数','胜/平/负','平均降时','中位降时','基线→新方法运行秒数中位数'], main_rows)}

验证48个实验臂均成功，每臂实际12次新评测，无评测失败和候选生成错误。P1图号009/048/071/088；P2/P3图号021/031/060/074/090/094。它们是本轮预留图，历史上并非从未看过；图数很少且固定一个种子，不能外推成全部100图或其他核数的改进。

P1的048从211661降到162275，088从170858降到106961，009持平。P2/P3在031都有退步：P2从273240变为277319，P3从273059变为276203。累计交付保留各方法实测最佳，因此这些实验负例不会污染最终方案。

## 新邻域是否真正贡献：同框架消融

local_legacy与integrated共用初始流程、WCC探测规则和总预算，前者后段使用旧邻域，后者加入新邻域并改变相应调用分配。这比较的是整套邻域组合与分配策略，不能拆成单个算子的独立因果效应。

{table(['场景','新组合相对旧邻域：胜/平/负','平均降时'], ablation_rows)}

P1联合邻域在验证后段13次调用中产生9次当前目标改善，支持继续发展这条路线。P2新增邻域比同框架旧邻域平均仅好0.0615%；P3六图最终成绩全部相同。P3的data在11次调用中有4次中途改善，但旧邻域最终也追平了这些结果；region的10次调用没有改善。不能把“中途曾改善”解释成最终方法优势。

P3 integrated在6张图上仅data候选生成就累计花25.20秒，而local_legacy的旧邻域生成累计4.89秒；这些是同机双进程测试的观察值，不是跨机器运行保证。当前P3新组合增加了开销，尚未带来额外终局收益，不应默认启用。

## 开发负例与修复

v1开发组P2/P3平均降时分别为−18.43%和−11.26%，说明过早局部精修会挤掉有价值的全局布局。v2恢复完整分量候选与配套WCC探测，v3再加入一次有条件的大窗口探测。最终开发组P2/P3平均改善2.77%和2.70%，P1沿用未改动的v1实现，开发平均改善14.82%。开发P1仍有043负例，不能只展示验证组正例。

三版开发结果均保留在“实验对照”中，验证只跑最终v3。v2/v3仅改P2/P3，开发P1无需重复计数。最终验证仍有031退步，修复没有消除所有预算分配问题。

## 24次预算的定向补测

只选开发图066/P1、046/P2、046/P3，属于预算敏感性案例，不增加验证样本量。总上限120秒、单次25秒。

{table(['场景/图','基线周期','新方法周期','实际调用：基线/新方法','新方法12→24次周期'], [
 ['P1/066',199671,189401,'24/24','198200 → 189401'],
 ['P2/046',77846,77846,'20/24','77846 → 77846'],
 ['P3/046',77846,69084,'20/24','77846 → 69084']])}

P2/P3基线在20次后候选池耗尽，不能写成双方都实际调用24次。三个新方法用时分别58.56、4.58、4.93秒。P3的046预算翻倍有作用，P2同图无作用；统一增加调用次数会浪费预算。该P1结果仍不及累计最好125828，P3也不及累计最好58678，短预算从头求解与长期搜索作品有明显差距。

## 累计作品新增7项

下表全部为五核，列出相对第十一轮的改进。“入库来源”是保留下来的成功记录来源，同分方法可能不止一个，不能当作唯一归因。

{table(['图','场景','原周期','新周期','降时','入库来源'], gain_rows)}

031的两项大提升来自强基线，018/P3也有基线同分，094两个场景新旧邻域同分。这些是作品实际改进，但不能全部归功于新增算法。以五核全部100张图为分母，相对上轮平均配对降时为P1 0%、P2 0.1281%、P3 0.1445%；2—4核本轮没有新纪录。

另做P2/P3跨场景复用6次检查，其中5次新评测、1次缓存命中，没有继续改善。094的两场景方案相同，因此无需重复迁移。

## 可复现性、成本与交付

在只复制源码、官方评测器、config与048/094原图的隔离目录中，运行公开solve.py入口，各12次全新评测，P1/048精确复现162275，P3/094精确复现16243。不含历史方案库和评测缓存。首次隔离目录漏带已有依赖“A题研究/探索”，导入阶段失败、尚未发生评测；补齐该源码目录后两例成功。迁移时必须保留solver、advanced_solver、精修求解器、直接迭代、探索这五个源码目录及官方附件路径结构。

{table(['批次','实验臂','新评测','失败','批次墙钟秒'], [[r['panel'],r['runs'],r['fresh_calls'],r['failed_calls'],f"{r['wall_seconds']:.2f}"] for r in costs])}

上述138臂共1720次新评测，包含开发修正，不能算作1720张独立图。加上隔离复现、跨场景补测和独立复评，本轮共{total_logical}次逻辑调用、{total_fresh}次新评测。63项相关单元测试通过，git diff --check通过；独立复评7/7一致，DAG检查1500/1500通过。所有本轮实验已结束，旧长队列仍保持暂停。

“方案”目录包含全部1500份可用JSON；“全部成绩.csv”“改善清单.csv”及“各核数汇总.csv”给出完整记录；“实验对照”保存分组比较、每次调用的改善曲线和耗时；“验证组邻域调用.csv”及候选生成耗时表用于检查预算效率。求解器入口为上级目录solve.py，具体复现命令见README.md。

## 下一步优先级

P1继续以联合拆分/分核为主要方向，先检验开发负例与更多未用于本轮调参的结构类型，再考虑改变默认方法。P2/P3优先改预算分配和候选生成成本：保留少量结构不同的近优父方案，给“分核→WCC”或“区域移动→缓存”组合保留必要机会，避免只围绕单一当前最佳继续搜索；根据实际改善幅度和生成耗时分配后续预算，压缩低价值data/region调用。

这些是下一轮待验证方案，尚未实现或证明有效。下一轮继续保持相同预算的强基线和旧邻域消融，先跑小型结构面板；有稳定收益再扩展，避免再次启动长时间盲目全量扫描。
'''
    (OUT/'第十二轮结果.md').write_text(note, encoding='utf-8')
    print(accounting)


if __name__ == '__main__': main()
