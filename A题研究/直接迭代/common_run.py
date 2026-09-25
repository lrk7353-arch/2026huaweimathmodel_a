"""Small execution helpers: existing successes + fresh official evaluations."""
import csv,json,sys,time
from pathlib import Path
R=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(R),str(R/'精修求解器')]
from solver.common import DATA,atomic_json,read_json
from solver.evaluator import evaluate
from solver.graph_ir import GraphIR
from solver.plan import validate_plan

def score(r):
 m=r['metrics'];return m['makespan'],m['data_movement_bytes']['added_copy_bytes']
def key(r):return Path(r['graph_path']).stem,r['problem'],r['metrics']['num_cores']
def known(include_direct=True):
 best={}
 def add(r):
  if not r or r.get('status')!='success' or r.get('problem') not in (1,2,3):return
  k=key(r)
  if k not in best or score(r)<score(best[k]):best[k]=r
 for p in (R/'当前最佳方案_v3_阶段快照').glob('p*/n*/*.provenance.json'):add(read_json(p)['evaluation_record'])
 for p in (R/'advanced_solver/runs/formal_v2').glob('*/slots/case_*/p*_n*/attempt_*/summary.json'):
  s=read_json(p)
  if s.get('completed') and s.get('best'):add(s['best']['record'])
 for p in (R/'实验记录/快速开发轮_v1').glob('case_*/summary.json'):
  s=read_json(p)
  if s.get('best_official_verified'):add(s['best']['record'])
 if not include_direct:return best
 for p in (R/'直接迭代/运行结果').glob('跨核*/summary.json'):
  for x in read_json(p)['results']:
   if x.get('accepted'):add(x['record'])
 for p in (R/'直接迭代/运行结果').glob('P1*/case_*/*/summary.json'):
  s=read_json(p)
  if s.get('best'):add(s['best']['record'])
 for p in (R/'直接迭代/运行结果').glob('P3*/case_*/summary.json'):
  s=read_json(p)
  if s.get('best_record'):add(s['best_record'])
 for p in (R/'直接迭代/运行结果').glob('P3*/case_*/*/summary.json'):
  add(read_json(p).get('best_record'))
 for p in (R/'直接迭代/运行结果').glob('P23*/case_*/p*_n*/*/summary.json'):
  add(read_json(p).get('best_record'))
 for p in (R/'直接迭代/运行结果').glob('P3读序归因*/case_*/summary.json'):
  for pair in read_json(p).get('records',{}).values():
   for record in pair.values():add(record)
 for p in (R/'直接迭代/运行结果').glob('P2五核对照*/case_*/summary.json'):
  s=read_json(p)
  if s.get('best'):add(s['best']['record'])
 for p in (R/'直接迭代/运行结果').glob('P3五核四格*/case_*/*.json'):
  if p.name.startswith('t'):add(read_json(p))
 for p in (R/'直接迭代/运行结果').glob('推广*/slots/case_*/p*_n*/summary.json'):
  add(read_json(p).get('best_record'))
 for p in (R/'直接迭代/运行结果').glob('深化*/slots/case_*/p*_n*/summary.json'):
  add(read_json(p).get('best_record'))
 for p in (R/'直接迭代/运行结果').glob('深化等预算*/case_*/*/seed*/summary.json'):
  add(read_json(p).get('best_record'))
 for p in (R/'直接迭代/运行结果').glob('融合*/case_*/*/seed*/summary.json'):
  add(read_json(p).get('best_record'))
 for p in (R/'直接迭代/运行结果').glob('融合*/slots/case_*/p*_n*/summary.json'):
  add(read_json(p).get('best_record'))
 for p in (R/'直接迭代/运行结果').glob('综合*/case_*/*/seed*/summary.json'):
  add(read_json(p).get('best_record'))
 for p in (R/'直接迭代/运行结果').glob('综合*/slots/case_*/p*_n*/summary.json'):
  add(read_json(p).get('best_record'))
 for p in (R/'直接迭代/运行结果').glob('区域搜索*/slots/case_*/p*_n*/*/summary.json'):
  add(read_json(p).get('best_record'))
 for p in (R/'直接迭代/运行结果').glob('端到端*/slots/case_*/p*_n*/*/summary.json'):
  add(read_json(p).get('best_record'))
 return best

def write_csv(p,rows):
 p.parent.mkdir(parents=True,exist_ok=True)
 if not rows:return
 with p.open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(dict.fromkeys(k for r in rows for k in r)));w.writeheader();w.writerows(rows)

def run_candidate(case,p,n,plan,evdir,timeout=None):
 graph=DATA/(case+'.json');ir=GraphIR.from_path(graph);validate_plan(ir,plan)
 if len(plan['core_schedules'])!=n:raise ValueError('wrong target core count')
 return evaluate(graph,plan,p,evdir,timeout=timeout or (180 if len(ir.compute_ids)>10000 else 60),config_path=DATA/'config.txt')
