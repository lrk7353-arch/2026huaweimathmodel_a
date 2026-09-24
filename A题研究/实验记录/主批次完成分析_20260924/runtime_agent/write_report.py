from pathlib import Path
import collections
import json

OUT=Path(__file__).resolve().parent
d=json.loads((OUT/'runtime_analysis.json').read_text())
rows=json.loads((OUT/'evaluation_runtime.json').read_text())
slots=json.loads((OUT/'slot_runtime_traffic.json').read_text())
stages=json.loads((OUT/'stage_transitions.json').read_text())

def rollup(rs):
    return {'count':len(rs),'fresh':sum(not r['cache_hit'] for r in rs),'cache_hits':sum(r['cache_hit'] for r in rs),
        'status_counts':dict(collections.Counter(r['status'] for r in rs)),
        'wrapper_seconds_sum':sum(r['wrapper_seconds'] for r in rs),
        'strict_improvement_events':sum(r['strict_improvement'] for r in rs),
        'strict_improvement_slots':len({r['slot'] for r in rs if r['strict_improvement']}),
        'lex_improvement_events':sum(r['lex_improvement'] for r in rs),
        'selected_final_count':sum(r['selected'] for r in rs)}

sup={'p1_balanced':rollup([r for r in rows if r['problem']==1 and r['name'].endswith('_p1_balanced')]),
     'p1_eft':rollup([r for r in rows if r['problem']==1 and r['name'].endswith('_p1_eft')]),
     'p3_reencode_control':rollup([r for r in rows if r['problem']==3 and r['name']=='cache_reencode_control']),
     'p3_cache_actions':rollup([r for r in rows if r['problem']==3 and r['stage']=='cache' and r['name']!='cache_reencode_control'])}
timeouts=[r for r in rows if r['status']=='timeout']
sup['timeouts']={'graphs':dict(collections.Counter(r['case'] for r in timeouts)),
                 'slots':len({r['slot'] for r in timeouts}),
                 'wrapper_seconds_sum':sum(r['wrapper_seconds'] for r in timeouts),
                 'minimum_compute_ops':min(r['compute_ops'] for r in timeouts)}
sup['stage_strict_improvement_slots']={f'p{p}_{st}':len({r['slot'] for r in stages if r['problem']==p and r['stage']==st and r['strict_improvement']})
   for p in [1,2,3] for st in ['operation','trace','cache']}
(OUT/'supplementary_rollups.json').write_text(json.dumps(sup,ensure_ascii=False,indent=2)+'\n')
lines=['# 三主批次完成后的耗时、候选收益与搬运诊断','',
'读取范围：formal_v2 的 full_p1_seed17、full_p2_seed17、full_p3_seed17，各100图×N2..5，共1200个完成配置。每份 summary 与 slot 完成状态相符，summary SHA、图 SHA 和计数均检查。此次分析没有运行新评测，也没有修改原始结果。原始 gzip 的独立全量校验由主任务负责。','',
'**结论：长时间主要消耗在少量大图上的重复官方评测，最值得调整的是候选预算和筛选，而非生成器速度。P1 的一个候选家族消耗显著却没有观测到增益；P2/P3 的后半段精修有实质贡献，不能统一截短到前12次。**','',
'## 统计口径','',
'- 本文累计秒数是并发求解进程或评测调用持续时间之和，绝不是用户实际等待的墙钟时间，也不是CPU指令执行时间。',
'- “逻辑调用”包括真实新评测、超时和精确缓存命中；缓存命中仍占算法预算，但只计本次查询/校验耗时。',
'- 缓存记录的 evaluation_elapsed_seconds 和 worker_elapsed_seconds 继承原始历史评测，计算本轮官方函数时间时已剔除。',
'- “候选贡献”按固定运行顺序的累计最优值统计，依赖已有前序候选；不是公平同预算消融或因果证明。',
'- 1200配置最终均有有效方案，不等于内部所有候选都成功。','',
'## 时间与失败分布','',
'| 场景 | 逻辑调用 | 新评测 / 缓存 | 候选超时 | 累计求解进程秒 | 单配置中位/P90秒 | 评测调用链占比 |',
'|---|---:|---:|---:|---:|---:|---:|']
for p,v in d['problems'].items():
    q=v['solver_elapsed_quantiles'];lines.append(f"| P{p} | {v['count']} | {v['fresh_calls']} / {v['cache_hits']} | {v['status'].get('timeout',0)} | {v['solver_elapsed_seconds_sum']:.1f} | {q['0.5']:.1f} / {q['0.9']:.1f} | {v['wrapper_seconds_sum']/v['solver_elapsed_seconds_sum']:.2%} |")
lines+=['',
f"三批次共21608次逻辑调用，19838次新评测、1770次缓存；123次超时全部在P1，涉及8图24个配置，超时调用累计 {sup['timeouts']['wrapper_seconds_sum']:.1f} 秒。P2/P3没有候选失败。P1失败候选未成为最终方案。",'',
'19张大图（计算算子数>10000）占19%的配置，却占P1/P2/P3累计求解时间的87.46%/74.11%/74.29%。同一张大图在不同核数、不同场景被重复评测，会造成明显长尾。P1最慢的 case_014 N3 单配置约2038秒。',
'',
'候选生成累计1775.8秒，仅占三个批次累计132899.9秒的1.34%。因此只优化候选生成，无法解决目前主要耗时。评测调用链包括官方函数、启动、数据加载、校验和结果保存；不能把约97%–99%的占比全部说成官方函数本身。','',
'## P1 可优先削减的低收益方向','',
f"P1 balanced 分配家族：1535次逻辑调用，其中1497成功、38超时；累计 {sup['p1_balanced']['wrapper_seconds_sum']:.1f} 秒（6.33累计进程小时），没有一次改进当时最优时间，也没有改善同时间下的搬运量，最终入选0次。P1 EFT 家族1665调用，47个配置改进时间，48个配置最终入选（其中1个只改善副指标）。",'',
'这意味着 balanced 是下一版优先做删减消融的对象。它是这批固定数据和顺序中的观测结论：如果未来生成器或排序改变，不能保证仍无用；不能把事后观察直接写成数学安全剪枝。建议冻结旧结果后，在独立声明的新配置中去除该家族并进行对应差分验证；省下的预算优先投入结构性P1改进。','',
'P1只有47/400配置从当前粗块 operation 方法得到时间收益，352/400最终仍来自Component/WCC。说明现有粗切法不是P1的主要突破口，应该围绕Task等待、重复读、spill与组件内M/V重排设计更有针对性的候选。已有p1_selective与WCC交错正负实验可作机制起点，不能直接把其个别精选图收益外推全体。','',
'## P2/P3 后续精修值得保留，缓存贡献需拆开','',
'P2：operation阶段160个配置改善时间，trace阶段228个配置改善时间；最终229个配置来自trace。P3：operation 167个、trace 209个、cache阶段232个配置改善时间。各阶段改善集合可重叠，不能相加当独立受益配置。','',
'P3 cache阶段包含重编码对照：cache_reencode_control有403次调用，在198个不同配置改善时间，106个最终方案来自该对照。真正缓存动作有1060次调用，在124个不同配置改善时间，128个最终方案来自这些动作（包含同时间副指标改进）。因此不能把cache阶段的232个受益配置全部归因于缓存感知；应继续通过固定映射/顺序对照识别缓存动作带来的额外贡献。','',
'## 为什么不能简单把全部预算砍半','',
'以下是看完本轮所有结果后的前缀回算，不是独立验证的停止规则。前缀含成功、超时和缓存逻辑调用。','',
'| 场景 / 只看前n次 | 最终时间完全相同 | 最坏时间劣化（相对本轮最终值） | 后续已发生评测链累计秒 |',
'|---|---:|---:|---:|']
for p,n in [('1','8'),('2','12'),('2','20'),('3','12'),('3','20')]:
    v=d['problems'][p]['prefix_hindsight'][n];lines.append(f"| P{p} / {n} | {v['same_makespan']}/400 | {v['max_makespan_regret_fraction']:.2%} | {v['post_prefix_wrapper_seconds']:.1f} |")
lines+=['',
'P1可研究更短初筛，但P2/P3前12次主要落在Component/Operation阶段，会遗漏真正有贡献的trace/cache。快速入口应直接从已验证强方案开始，给精修阶段留预算，而不是每次把基线重跑一遍。','',
'## 搬运量与时间的权衡','',
'| 场景 | 较本轮Component更快的配置 | 其中额外搬运增/减/不变 | 最终存在spill的配置 |',
'|---|---:|---:|---:|']
for p,v in d['problems'].items():
    t=v['traffic_among_improved_over_component'];lines.append(f"| P{p} | {t['n']} | {t['more_added_copy']} / {t['less_added_copy']} / {t['same_added_copy']} | {v['best_final_spill_positive']} / 400 |")
lines+=['',
'更快不一定意味着更少搬运：P2的257个时间改善配置中182个增加额外搬运，P3的318个中193个增加额外搬运。这符合先优化时间、同时间再比搬运的选择口径，也说明不能用“通信量越低就越好”的单一代理替代官方评测。后续要结合关键路径上的等待、DDR竞争和spill位置做筛选；总字节数本身不是因果解释。','',
'## 下一步优先顺序','',
'1. 优先改P1候选分配：balanced删减做声明清楚的差分验证；Task/重复输入/缓冲spill的可靠必要下界可用于安全剪枝，普通代理只用于排序。',
'2. 快速开发继续使用固定12图面板和已有强方案，每轮只支付新增候选；机制有收益才扩到24图和少量大压力图。',
'3. P2/P3保留trace/cache精修预算，特别补P3重编码与真正缓存动作的对照；不要机械重复旧4700项矩阵。',
'4. 方法稳定后做一次冻结的全量检查、关键消融和跨核数一致性验证；当前结果复用为对照。',
'',
'证据：runtime_analysis.json 含分场景、阶段、候选名称和前缀统计；slot_runtime_traffic.json 为逐配置；evaluation_runtime.json 为逐逻辑调用；source_manifest.json 为全部1200份输入摘要路径和SHA；supplementary_rollups.json 为本文候选家族及超时汇总。']
(OUT/'耗时与后续优先级.md').write_text('\n'.join(lines)+'\n')
print(str(OUT/'耗时与后续优先级.md'))
