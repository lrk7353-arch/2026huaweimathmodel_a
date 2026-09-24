from common_run import *
import statistics
O=R/'直接迭代/本轮成果';D=R/'直接迭代/运行结果';s=read_json(O/'summary.json')
lines=['# 本轮直接迭代结果','',f'本轮实验已结束，成果已导出。当前方案覆盖 {s["coverage"]}/1500 个组合；相对本轮开始时最好方案，严格降时 {s["strict_time_improved"]} 个配置，另有 {s["objective_improved"]-s["strict_time_improved"]} 个配置耗时持平、额外复制字节减少。以下累计方案库成绩与固定预算方法比较分别列出。','',
'## 本轮前后：100图五核平均加速比','', '| 场景 | 本轮开始 | 本轮结束 | 严格改善图数 |','|---|---:|---:|---:|']
for g in s['five_core']:lines.append(f'| P{g["problem"]} | {g["before_mean_speedup"]:.6f} | {g["after_mean_speedup"]:.6f} | {g["improved_cases"]}/100 |')
lines+=['','以上分子均为原官方单核时间，逐图求比再平均；本轮开始方案已经包含历史额外探索与v2成果，不是仅用旧v2成绩作分母。当前成绩为累计最好方案库，不是同预算算法排名。','',
'## 代表性改进','', '| 配置 | 本轮前官方耗时 | 本轮后官方耗时 | 降时 |','|---|---:|---:|---:|']
with (O/'全部成绩.csv').open(encoding='utf-8-sig') as f:all_rows=list(csv.DictReader(f))
for problem,case in [('1','case_082'),('1','case_062'),('2','case_044'),('3','case_049')]:
 r=next(r for r in all_rows if r['problem']==problem and r['case']==case and r['cores']=='5')
 lines.append(f'| P{problem} / {case} / 五核 | {r["before"]} | {r["after"]} | {float(r["reduction_pct"]):.2f}% |')
lines+=['','这些是官方模拟耗时，不是求解器实际运行秒数。全部逐配置记录见改善清单.csv；未改善配置继续使用已有最好方案。','',
'## 执行批次','', '| 批次 | 范围 | 新评测/逻辑调用 | 本批观察墙时 |','|---|---|---:|---:|']
for name in ['跨核继承_v1','P1开发_v1','P1开发_v2','P1扩展_v2','P1大图_v2','P1余图精修_v2','P3精修_v1','P3扩展_v1','P2五核对照_v1','P3五核四格_v1']:
 f=D/name/'summary.json'
 if not f.exists():continue
 d=read_json(f);rows=d.get('rows',[])
 logical=d.get('logical_calls',sum(r.get('logical_calls',0) for r in rows));fresh=d.get('new_calls',sum(r.get('new_calls',0) for r in rows));wall=d.get('wall_seconds',d.get('elapsed_seconds',0))
 desc=str(len(rows))+'个返回记录' if rows else str(d.get('proposals',d.get('complete_cases','?')))+'个目标'
 lines.append(f'| {name} | {desc} | {fresh}/{logical} | {wall:.2f}s |')
lines+=['','各批曾并发运行，墙时不能相加当总等待时长。旧记录复用和缓存命中不算新评测；P1开发v2继承首轮缓存，其秒数不能当冷启动提速倍数。','',
'## P1质量和边界','']
for name in ['P1开发_v2','P1扩展_v2','P1大图_v2']:
 d=read_json(D/name/'summary.json');v=d['rows'];ok=[r for r in v if r['new_time'] is not None]
 w=sum(r['new_time']<r['old_prefix_time'] for r in ok);l=sum(r['new_time']>r['old_prefix_time'] for r in ok)
 lines.append(f'- {name}：相对旧版前8次记录，{w}胜、{len(ok)-w-l}平、{l}负；{len(v)-len(ok)}个配置本次从头搜索未得到可行结果。')
lines+=['','开发首版在case044出现回退，随后保留粗切分并按重分量结构选择细化，第二版修复开发组退化。扩展组case075仍约0.32%回退，保留负例，不用弱新结果替换已有好方案。压力组case014在240秒从头预算内无解，随后warm追加精修保留并改善了旧可行方案；case062在240秒窗口内取得38.02%降时，但未耗尽候选，标为时间停止。','',
'追加精修的初始已有方案不冒充免费从头求解。P1已在全100图上进行冷启动面板或追加精修尝试，预算不统一，不能据此声称全100图同预算优势。','',
'## P2强对照','']
f=s['fair_P2_N5'];lines.append(f'五核已齐 {f["coverage"]}/100；旧v2全方法相对强Component为{f["wins"]}胜/{f["ties"]}平/{f["losses"]}负。')
if f['coverage']==100:lines.append(f'100图平均逐图加速比：强Component {f["component_mean_speedup"]:.6f}，旧v2全方法 {f["full_mean_speedup"]:.6f}。二者逻辑调用上限均24，实际调用可不同；不比较共享缓存和并发状态下的冷启动墙时。')
lines+=['','## P3四格：固定计划区分硬件与策略','']
d=read_json(D/'P3五核四格_v1/summary.json');rows=d['rows']
if d['complete']:
 for a,b,label in [('t2_pi2','t3_pi2','固定P2计划切换缓存场景'),('t3_pi2','t3_pi3','P3场景中换成P3搜索选中的计划'),('t2_pi2','t3_pi3','场景与选中计划共同变化')]:
  gains=[100*(1-r[b]/r[a]) for r in rows];rat=[r[a]/r[b] for r in rows]
  lines.append(f'- {label}：{sum(x>0 for x in gains)}胜/{sum(x==0 for x in gains)}平/{sum(x<0 for x in gains)}负；平均降时 {statistics.mean(gains):.4f}%，平均逐图比值 {statistics.mean(rat):.6f}。')
lines+=['','这里的pi2/pi3是旧v2既定计划，用于解释该轮结果；追加精修成果不反向替换四格选择。不同平均降时不能相加。case049旧P3退化来自计划选择：相同P2计划放入P3得到59564，比原59914略好；旧P3自己选择的计划却是86100。追加精修将当前最好80811改善到59456，主要来自复用P2计划。','',
'## 使用','',
'- 全部成绩.csv：本轮前后逐配置成绩、方案位置、官方记录。',
'- 改善清单.csv：所有目标改善记录。',
'- 五核强对照.csv：全100图公平上限比较；不混入累计方案库。',
'- 方案/p*/n*：原官方两字段JSON。',
'- ../solve.py：当前五核运行入口；../README.md含命令。',
'- 此目录为本地成果，没有重新打包之前的队友ZIP。',
'- 当前覆盖：2—5核共1200个组合齐全；1核已有27个，还缺273个。本轮新方法主要验证五核，不能称为1500份最终完整交付。','',
'## 后续优先级','',
'1. 把本轮有效的跨核继承和选择性精修推广到2—4核，继续按短窗口、官方择优推进。',
'2. 对P1大图优化候选生成和单次评测开销；case014从头搜索超时、case075小幅退化仍是需要解决的边界。',
'3. 补齐1核覆盖与关键消融。P3后续把缓存引导的独立收益与P2方案复用收益继续分开；当前case049的大收益不能写成纯缓存优化贡献。',
'4. 当前方法对照仅使用seed17；稳定性和不同预算的结论仍需有针对性的补充，不自动恢复旧的4700项长队列。']
(O/'本轮结果.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(O/'本轮结果.md')
