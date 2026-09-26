"""Offline validation: exact coverage, legal plans, metrics and manuscript anchors."""
from pathlib import Path
import ast,csv,json,tarfile,hashlib,sys,statistics
R=Path(__file__).resolve().parents[2];O=R/'论文主体版本_v1_20260926';sys.path.insert(0,str(R/'A题研究'))
from solver.graph_ir import GraphIR
from solver.plan import validate_plan
rows=list(csv.DictReader((O/'最终方案1500/累计1500配置成绩.csv').open(encoding='utf-8-sig')))
expected={(f'case_{i:03}',p,k) for i in range(1,101) for p in range(1,4) for k in range(1,6)}
key=lambda r:(r['case'],int(r['problem']),int(r['cores']))
assert len(rows)==1500 and {key(r) for r in rows}==expected
with tarfile.open(O/'最终方案1500/selected_plans.tar.gz') as a:plans={m.name:a.extractfile(m).read() for m in a if m.isfile()}
assert len(plans)==1500
bycase={}
for r in rows:bycase.setdefault(r['case'],[]).append(r)
for case,sub in bycase.items():
 ir=GraphIR.from_path(R/'选题分析/A题附件/data'/f'{case}.json')
 for r in sub:
  b=plans[r['plan']];assert hashlib.sha256(b).hexdigest()==r['paper_plan_sha256'];p=json.loads(b);validate_plan(ir,p);assert len(p['core_schedules'])==int(r['cores'])
  assert abs(float(r['speedup'])-float(r['original_singlecore'])/int(r['makespan']))<1e-10
anchors=list(csv.DictReader((O/'代码行号与论文章节.csv').open(encoding='utf-8-sig')))
for a in anchors:
 text=(R/a['file']).read_text();assert 1<=int(a['start'])<=int(a['end'])<=len(text.splitlines())
 if a['function']!='文件入口/完整模块':
  nodes=[n for n in ast.walk(ast.parse(text)) if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name==a['function']];assert len(nodes)==1 and nodes[0].lineno==int(a['start']) and nodes[0].end_lineno==int(a['end'])
metrics=list(csv.DictReader((O/'数据表/附录A_1500配置完整指标.csv').open(encoding='utf-8-sig')));assert len(metrics)==1500
lookup={key(r):r for r in rows}
for m in metrics:
 r=lookup[key(m)];assert int(r['makespan'])==int(m['makespan']) and int(r['added_copy'])==int(m['added_copy'])
 assert int(m['added_copy'])==int(m['partition_copy'])+int(m['spill_copy'])
 if int(m['problem'])==3:
  h=int(m['cache_hit_bytes']);miss=int(m['cache_miss_bytes']);assert abs(float(m['cache_byte_hit_rate'])-(h/(h+miss) if h+miss else 0))<1e-9
 else:assert m['cache_byte_hit_rate']=='N/A'
pairs=list(csv.DictReader((O/'数据表/附录B_P3同核500组.csv').open(encoding='utf-8-sig')));assert len(pairs)==500
summary={'plans':1500,'graphs':100,'valid_plan_coverage':True,'metric_rows':len(metrics),'p3_pairs':len(pairs),'code_anchors':len(anchors),'official_searches_started':0,'scope':'offline checks; inherited official evidence plus two separately evaluated transfers'}
(O/'离线验收.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(summary,ensure_ascii=False))
