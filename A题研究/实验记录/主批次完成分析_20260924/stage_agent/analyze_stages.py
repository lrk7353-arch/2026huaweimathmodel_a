import json, statistics, hashlib
from collections import Counter, defaultdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
OUT=Path(__file__).resolve().parent
RUNS=ROOT/'advanced_solver/runs/formal_v2'
def stat(xs):
 return {'mean':statistics.mean(xs) if xs else None,'median':statistics.median(xs) if xs else None,'max':max(xs) if xs else None}
def key(e):
 m=e['record']['metrics'];return (m['makespan'],m['data_movement_bytes']['added_copy_bytes'])
def brief(e):
 return {'name':e['name'],'stage':e['stage'],'makespan':key(e)[0],'added_copy_bytes':key(e)[1],'plan_sha256':e['plan_sha256'],'mechanism':e.get('metadata',{}).get('mechanism'), 'is_reencoding_control':e.get('metadata',{}).get('is_reencoding_control'), 'record_path':e['record'].get('record_path')}
def pct(a,b):return (a-b)/a*100
rows=[];files=[]
for p in (1,2,3):
 paths=sorted((RUNS/f'full_p{p}_seed17/slots').glob('case_*/*/attempt_*/summary.json'))
 assert len(paths)==400,(p,len(paths))
 for path in paths:
  raw=path.read_bytes();s=json.loads(raw);files.append({'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()})
  assert s['completed'] and s['status']=='success'
  previous=None;row={'case':s['case'],'problem':p,'num_cores':s['num_cores'],'stages':{},'best':brief(s['best']),'elapsed_seconds':s['elapsed_seconds'],'summary_path':str(path)}
  for phase in ('component','operation','trace','cache'):
   es=[e for e in s['evaluations'] if e['stage']==phase];good=[e for e in es if e['record']['status']=='success'];phbest=min(good,key=key) if good else None
   current=min([e for e in [previous,phbest] if e],key=key) if previous or phbest else None
   d={'evaluated':len(es),'status_counts':dict(Counter(e['record']['status'] for e in es)),'fresh_calls':sum(not e['record'].get('cache_hit') for e in es),'cache_hits':sum(bool(e['record'].get('cache_hit')) for e in es),'record_elapsed_seconds':sum(e['record'].get('elapsed_seconds',0) for e in es),'before':brief(previous) if previous else None,'best_in_stage':brief(phbest) if phbest else None,'after':brief(current) if current else None,'strict_makespan_improved':bool(previous and current and key(current)[0]<key(previous)[0]),'copy_only_improved':bool(previous and current and key(current)[0]==key(previous)[0] and key(current)[1]<key(previous)[1]),'relative_makespan_reduction_pct':pct(key(previous)[0],key(current)[0]) if previous and current else None}
   if phase=='cache':
    cs=[e for e in good if e.get('metadata',{}).get('is_reencoding_control')];ns=[e for e in good if not e.get('metadata',{}).get('is_reencoding_control')]
    control=min(cs,key=key) if cs else None; noncontrol=min(ns,key=key) if ns else None
    controlbase=min([e for e in [previous,control] if e],key=key)
    d['control']=brief(control) if control else None;d['noncontrol']=brief(noncontrol) if noncontrol else None
    d['control_strict_vs_before']=bool(control and previous and key(control)[0]<key(previous)[0])
    d['noncontrol_strict_vs_before']=bool(noncontrol and previous and key(noncontrol)[0]<key(previous)[0])
    d['noncontrol_strict_vs_control_and_before']=bool(noncontrol and key(noncontrol)[0]<key(controlbase)[0])
    d['gain_beyond_control_pct']=pct(key(controlbase)[0],key(current)[0])
    d['candidates']=[brief(e) for e in good]
   row['stages'][phase]=d;previous=current
  assert key(previous)==key(s['best']),(path,key(previous),key(s['best']))
  row['total_vs_component_pct']=pct(row['stages']['component']['after']['makespan'],key(previous)[0])
  rows.append(row)
report={'scope':'100 graphs × 4 requested core counts (2–5) × 3 problems; full seed 17; cumulative within-run attribution, not equal-budget independent ablation','per_problem':{},'input_summaries':files}
for p in (1,2,3):
 rs=[r for r in rows if r['problem']==p];pout={'count':len(rs),'total_vs_component_pct':stat([r['total_vs_component_pct'] for r in rs]),'total_strict_improvements':sum(r['total_vs_component_pct']>0 for r in rs),'final_stage_counts':dict(Counter(r['best']['stage'] for r in rs)),'stages':{}}
 for phase in ('component','operation','trace','cache'):
  ds=[r['stages'][phase] for r in rs];active=[d for d in ds if d['evaluated']]
  improved=[d for d in ds if d['strict_makespan_improved']]
  pout['stages'][phase]={'configuration_count':len(rs),'active_configuration_count':len(active),'strict_makespan_improvements':len(improved),'copy_only_improvements':sum(d['copy_only_improved'] for d in ds),'relative_gain_pct_all':stat([d['relative_makespan_reduction_pct'] for d in ds if d['relative_makespan_reduction_pct'] is not None]),'relative_gain_pct_improved':stat([d['relative_makespan_reduction_pct'] for d in improved]),'evaluations':sum(d['evaluated'] for d in ds),'fresh_calls':sum(d['fresh_calls'] for d in ds),'cache_hits':sum(d['cache_hits'] for d in ds),'record_elapsed_seconds':sum(d['record_elapsed_seconds'] for d in ds),'status_counts':dict(sum((Counter(d['status_counts']) for d in ds),Counter())),'by_num_cores':{str(n):{'count':100,'strict_makespan_improvements':sum(r['stages'][phase]['strict_makespan_improved'] for r in rs if r['num_cores']==n),'mean_gain_pct':statistics.mean([r['stages'][phase]['relative_makespan_reduction_pct'] or 0 for r in rs if r['num_cores']==n])} for n in (2,3,4,5)},'top_improvement_cases':[{'case':r['case'],'num_cores':r['num_cores'],**r['stages'][phase]} for r in sorted(rs,key=lambda r:r['stages'][phase]['relative_makespan_reduction_pct'] or 0,reverse=True)[:8]]}
  if phase=='cache':
   for k in ('control_strict_vs_before','noncontrol_strict_vs_before','noncontrol_strict_vs_control_and_before'):pout['stages'][phase][k]=sum(d.get(k,False) for d in ds)
   pout['stages'][phase]['gain_beyond_control_pct_all']=stat([d.get('gain_beyond_control_pct',0) for d in ds])
   pout['stages'][phase]['noncontrol_mechanism_wins']=dict(Counter(d['noncontrol']['mechanism'] for d in ds if d.get('noncontrol_strict_vs_control_and_before')))
 report['per_problem'][str(p)]=pout
(OUT/'stage_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
(OUT/'stage_rows.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
for p,ps in report['per_problem'].items():
 print('P'+p,'total',ps['total_strict_improvements'],ps['total_vs_component_pct'],'final_stages',ps['final_stage_counts'])
 for ph,d in ps['stages'].items():
  print(ph,{k:v for k,v in d.items() if k not in ('top_improvement_cases','by_num_cores')})
