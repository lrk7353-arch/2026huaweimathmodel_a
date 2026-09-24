"""Export official successful best plans and a concise comparison; no packaging."""
import argparse,shutil,statistics
from common_run import *

def main(a):
 out=Path(a.out).resolve();out.mkdir(parents=True,exist_ok=False);before=known(False);after=known();rows=[];changes=[]
 # Also include the P2 CLI result if it improved its incumbent.
 for p in (R/'直接迭代/运行结果').glob('P2入口*/case_*/summary.json'):
  s=read_json(p);r=s.get('best_record')
  if r and score(r)<score(after[key(r)]):after[key(r)]=r
 for k,r in sorted(after.items()):
  case,p,n=k;target=out/'方案'/f'p{p}'/f'n{n}'/(case+'_multicore_res.json');target.parent.mkdir(parents=True,exist_ok=True)
  shutil.copyfile(r['plan_path'],target)
  b=before.get(k);row={'case':case,'problem':p,'cores':n,'before':score(b)[0] if b else None,'after':score(r)[0],
    'reduction_pct':100*(1-score(r)[0]/score(b)[0]) if b else None,'added_copy_bytes':score(r)[1],
    'plan':str(target.relative_to(out)),'official_record':r['record_path'],'official_result':r['result_path']}
  rows.append(row)
  if b and score(r)<score(b):changes.append(row)
 write_csv(out/'全部成绩.csv',rows);write_csv(out/'改善清单.csv',changes)
 summary={'coverage':len(rows),'strict_time_improved':sum(x['after']<x['before'] for x in changes),'objective_improved':len(changes),'five_core':[]}
 for p in (1,2,3):
  rr=[x for x in rows if x['problem']==p and x['cores']==5];speeds0=[];speeds1=[]
  for x in rr:
   baseline=read_json(R/f'solver/runs/full_initial_v1/results/{x["case"]}/singlecore.json')['metrics']['makespan'];speeds0.append(baseline/x['before']);speeds1.append(baseline/x['after'])
  summary['five_core'].append({'problem':p,'cases':len(rr),'before_mean_speedup':statistics.mean(speeds0),'after_mean_speedup':statistics.mean(speeds1),'improved_cases':sum(x['after']<x['before'] for x in rr)})
 # Complete strong baseline comparison remains separate from cumulative portfolio.
 comp={}
 for p in (R/'advanced_solver/runs/formal_v2/component_p2_seed17/slots').glob('case_*/p2_n5/attempt_*/summary.json'):
  s=read_json(p)
  if s.get('completed') and s.get('best'):comp[s['case']]=s['best']['record']
 for p in (R/'直接迭代/运行结果/P2五核对照_v1').glob('case_*/summary.json'):
  s=read_json(p)
  if s.get('completed') and s.get('best'):comp[s['case']]=s['best']['record']
 fair=[]
 for c,r in sorted(comp.items()):
  f=read_json(R/f'advanced_solver/runs/formal_v2/full_p2_seed17/slots/{c}/p2_n5/attempt_0001/summary.json')['best']['record'];b=read_json(R/f'solver/runs/full_initial_v1/results/{c}/singlecore.json')['metrics']['makespan']
  fair.append({'case':c,'component':score(r)[0],'full_v2':score(f)[0],'component_speedup':b/score(r)[0],'full_speedup':b/score(f)[0]})
 write_csv(out/'五核强对照.csv',fair)
 summary['fair_P2_N5']={'coverage':len(fair),'wins':sum(x['full_v2']<x['component'] for x in fair),'ties':sum(x['full_v2']==x['component'] for x in fair),'losses':sum(x['full_v2']>x['component'] for x in fair)}
 if len(fair)==100:summary['fair_P2_N5'].update(component_mean_speedup=statistics.mean(x['component_speedup'] for x in fair),full_mean_speedup=statistics.mean(x['full_speedup'] for x in fair))
 atomic_json(out/'summary.json',summary)
 print(json.dumps(summary,ensure_ascii=False,indent=2))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--out',required=True);main(p.parse_args())
