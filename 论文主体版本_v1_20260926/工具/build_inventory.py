"""Complete tracked-file inventory, with per-file role and paper placement."""
from pathlib import Path
import subprocess,ast,csv,collections
R=Path(__file__).resolve().parents[2];O=R/'论文主体版本_v1_20260926'
files=[f for f in subprocess.check_output(['git','ls-files','-z'],cwd=R).decode().split('\0') if f]
curated={r['file']:r for r in csv.DictReader((O/'代码行号与论文章节.csv').open(encoding='utf-8-sig'))}
rows=[];groups=collections.defaultdict(lambda:[0,0])
for f in sorted(files):
 p=R/f
 if not p.is_file():continue
 ext=''.join(p.suffixes);role='';section='附录D：研究过程与追溯';use='仅在需要支持对应论断时引用；不得将历史版本数字当最新';symbols=''
 if f in curated:
  c=curated[f];role=c['functionality'];section=c['section'];use=c['placement']
 elif f.startswith('选题分析/A题附件/data/case_'):role='官方测试计算图：操作、张量、依赖';section='§2数据与附录输入';use='官方输入，不改写'
 elif f.startswith('选题分析/A题附件/code/'):role='官方评估/核内调度代码';section='§3资源语义与附录C';use='定义与校验依据，非自研贡献'
 elif '/plans/' in f or '/方案/' in f or f.endswith('_multicore_res.json'):role='该路径场景/核数/图对应的合法方案载荷';section='附录A/B对应配置';use='需与同版本记录配对，不从文件名推成绩'
 elif '/tests/' in f or p.name.startswith('test_'):role='正确性/回归测试';section='附录C验证';use='测试通过不等同优化效果证明'
 elif ext.endswith('.py'):role='求解、候选生成、实验运行或统计辅助代码';use='结合函数列表与模块说明判断用途，不自动纳入默认流程'
 elif ext in ('.csv',):role='逐配置/逐调用/汇总实验数据表';use='按表头及同目录报告识别样本、单位、版本，保留失败'
 elif ext.endswith('.json.gz') or ext.endswith('.jsonl.gz'):role='压缩原始评估/调用/轨迹证据';use='用gzip读取；优先核对status与输入来源'
 elif ext.endswith('.json'):role='实验协议、方案或结构化结果记录';use='以实际字段和同目录说明为准；绝对路径为历史来源'
 elif ext.endswith(('.tar.gz','.part000','.part001','.part002')):role='方案/源码/实验原始记录归档或分卷';use='先按对应README重组/解包，不将压缩包本身当统计表'
 elif ext in ('.png','.pdf','.svg'):role='实验图表或文档可视化';section='对应实验章节/附录';use='与生成脚本和原始表配对；旧图不替换最新数据'
 elif ext in ('.md','.txt'):role='题意、方法、实验结论、协议或复现说明';use='按标题定位；历史计划不表示已完成'
 elif ext in ('.sh','.command'):role='命令行启动/进度工具';section='附录C运行';use='原主机入口可能需配置；不作为算法理论'
 elif f.startswith('tools/ascend910b/'):role='昇腾真机机制实验源代码/构建文件';section='§6机制解释/附录D';use='与官方模拟参数分开'
 else:role='项目配置或附件';section='附件';use='保留复现所需上下文'
 if ext=='.py':
  try:
   t=ast.parse(p.read_text());doc=ast.get_docstring(t) or '';symbols='; '.join(f'{n.name}:L{n.lineno}-{n.end_lineno}' for n in t.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)))
   if doc and f not in curated:role+='；'+doc.splitlines()[0][:240]
  except (SyntaxError,UnicodeError):symbols='无法静态解析；需人工检查'
 elif ext=='.md':
  try:
   first=next((s.lstrip('# ').strip() for s in p.read_text().splitlines() if s.strip()),'');role+='：'+first[:180]
  except UnicodeError:pass
 elif ext=='.csv':
  try:
   with p.open(encoding='utf-8-sig') as fh:symbols=fh.readline().strip()[:1000]
  except UnicodeError:pass
 if '论文主体版本_v1_20260926/数据表' in f:section='§7与附录A/B';use='本版统一结果源，最新口径以03说明为准'
 if '/待优化冷启动/' in f:section='§8.2 CS-1000占位';use='未完成，不宣称1000全量优势'
 rows.append(dict(file=f,bytes=p.stat().st_size,role=role,functions_or_fields=symbols,paper_location=section,usage_boundary=use))
 group='/'.join(Path(f).parts[:3]);groups[group][0]+=1;groups[group][1]+=p.stat().st_size
with (O/'全项目文件功能索引.csv').open('w',encoding='utf-8-sig',newline='') as fh:
 w=csv.DictWriter(fh,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
lines=['# 完整项目结构与逐文件功能目录',f'当前Git索引逐文件扫描得到{len(rows)}项。完整路径、大小、功能、Python顶层函数行号/CSV表头、论文位置及使用边界均在[全项目文件功能索引.csv](全项目文件功能索引.csv)。关键文件的具体行文衔接见02；压缩归档内部的历史文件另见Release文件索引。通用历史文件分类仅做导航，不能替代读取其实际报告。','''```text
仓库根/
├── 论文主体版本_v1_20260926/    本次写作主入口、表图、最优方案、待优化占位、验证工具
├── 选题分析/A题附件/
│   ├── data/                  官方100图与固定config
│   ├── code/                  原官方评估器、核内调度
│   └── docs/                  官方解释资料
├── A题研究/
│   ├── solver/                图解析、方案校验、官方调用、基础求解
│   ├── advanced_solver/       组件、操作分配、轨迹及缓存候选
│   ├── 精修求解器/            历史及成熟候选/控制器依赖
│   ├── 直接迭代/              发布入口、场景管线、各轮实验与交付
│   ├── 实验记录/              早期对照、四格、统计与协议
│   ├── 真机机制研究_20260926/  昇腾机制实验；不替代题设模拟
│   ├── 方案审阅/              历史问题辨析与微实验
│   ├── 探索/                  探索代码与报告，非全部进入默认流程
│   └── 论文草稿/              旧稿，只作历史参考；本次按主体版本重写
└── tools/ascend910b/           真机实验构建与运行支持
```''','## 所有目录的前三层汇总（完整深层路径见逐文件CSV）','|目录/路径前缀|文件数|字节|','|---|---:|---:|']
for g,(n,b) in sorted(groups.items()):lines.append(f'|{g}|{n}|{b}|')
(O/'07_完整项目结构与逐文件目录.md').write_text('\n\n'.join(lines[:3])+'\n\n'+'\n'.join(lines[3:]),encoding='utf-8')
print('indexed',len(rows))
