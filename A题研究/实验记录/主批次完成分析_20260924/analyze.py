#!/usr/bin/env python3
"""Read-only analysis. Paired graph-wise ratios; no evaluations and no portfolio mixing."""
import csv,hashlib,json,math,statistics as st
from collections import Counter
from pathlib import Path
R=Path(__file__).resolve().parents[2];O=Path(__file__).resolve().parent
F=R/'advanced_solver/runs/formal_v2'
def load(p):return json.loads(p.read_text())
def mean(v):return st.mean(v)
def dump(p,d):p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
base={f'case_{i:03d}':load(R/f'solver/runs/full_initial_v1/results/case_{i:03d}/singlecore.json')['metrics']['makespan'] for i in range(1,101)}
with (R/'方案审阅/结构核验/a_graph_structure.csv').open(encoding='utf-8-sig') as f:struct={r['case']:r for r in csv.DictReader(f)}
rows=[];sources={}
for p in (1,2,3):
 for file in sorted((F/f'full_p{p}_seed17/slots').glob('case_*/p*_n*/attempt_*/summary.json')):
  b=file.read_bytes();s=json.loads(b);assert s['completed'] and s['state']=='finished'
  sources[str(file.relative_to(R))]=hashlib.sha256(b).hexdigest()
  m=s['best']['record']['metrics'];dm=m['data_movement_bytes'];c=s['case'];n=s['num_cores'];t=m['makespan'];lb=s['lower_bound']['value'];g=struct[c]
  rows.append(dict(case=c,problem=p,cores=n,makespan=t,baseline=base[c],speedup=base[c]/t,
   lower_bound=lb,lb_ratio=t/lb,upper_possible_reduction_pct=100*(1-lb/t),active_cores=m['active_cores'],
   compute_ops=int(g['compute_ops']),largest_component_work_M_fraction=float(g['largest_component_work_M_fraction']),
   original_copy_bytes=dm['original_graph_copy_bytes'],added_copy_bytes=dm['added_copy_bytes'],spill_bytes=dm['spill_added_copy_bytes'],
   stage=s['best']['stage'],name=s['best']['name'],logical_calls=s['evaluated_count'],new_calls=s['official_calls'],
   cache_hits=s['cache_hits'],elapsed=s['elapsed_seconds'],summary_path=str(file)))
assert len(rows)==1200 and len({(r['case'],r['problem'],r['cores']) for r in rows})==1200
ix={(r['case'],r['problem'],r['cores']):r for r in rows}
agg=[]
for p in (1,2,3):
 for n in (2,3,4,5):
  v=[r for r in rows if r['problem']==p and r['cores']==n];sp=[r['speedup'] for r in v]
  agg.append(dict(problem=p,cores=n,count=len(v),mean_speedup=mean(sp),median_speedup=st.median(sp),
   geometric_mean_speedup=math.exp(mean(map(math.log,sp))),minimum_speedup=min(sp),maximum_speedup=max(sp),
   slower_than_single=sum(x<1 for x in sp),within_5pct_of_lb=sum(r['lb_ratio']<=1.05 for r in v),
   within_10pct_of_lb=sum(r['lb_ratio']<=1.1 for r in v),active_cores=dict(Counter(r['active_cores'] for r in v)),
   stage_winners=dict(Counter(r['stage'] for r in v))))
monotonic=[]
for p in (1,2,3):
 for n in (3,4,5):
  pairs=[]
  for c in base:
   a,b=ix[c,p,n-1],ix[c,p,n];pairs.append(dict(case=c,previous=a['makespan'],current=b['makespan'],change_pct=100*(b['makespan']/a['makespan']-1)))
  monotonic.append(dict(problem=p,cores_transition=[n-1,n],worse=sum(r['current']>r['previous'] for r in pairs),equal=sum(r['current']==r['previous'] for r in pairs),better=sum(r['current']<r['previous'] for r in pairs),largest_regressions=sorted(pairs,key=lambda r:r['change_pct'],reverse=True)[:8]))
core_diagnostic=[]
for p in (1,2,3):
 v=[]
 for c in base:
  current=ix[c,p,5];best=min((ix[c,p,n] for n in (2,3,4,5)),key=lambda r:r['makespan'])
  if best['makespan']<current['makespan']:v.append(dict(case=c,best_core_count=best['cores'],best_lower_core_time=best['makespan'],five_core_time=current['makespan'],potential_reduction_pct=100*(1-best['makespan']/current['makespan'])))
 core_diagnostic.append(dict(problem=p,count=len(v),cases=sorted(v,key=lambda r:r['potential_reduction_pct'],reverse=True),note='Screening only: padding/replaying under target core count is required before claiming a realizable improvement.'))
cross=[]
for a,b in [(1,2),(2,3)]:
 for n in (2,3,4,5):
  v=[]
  for c in base:
   t1,t2=ix[c,a,n]['makespan'],ix[c,b,n]['makespan'];v.append(dict(case=c,first_time=t1,second_time=t2,ratio=t1/t2,reduction_pct=100*(1-t2/t1)))
  cross.append(dict(from_problem=a,to_problem=b,cores=n,count=100,mean_paired_ratio=mean([r['ratio'] for r in v]),median_paired_ratio=st.median(r['ratio'] for r in v),wins=sum(r['ratio']>1 for r in v),ties=sum(r['ratio']==1 for r in v),losses=sum(r['ratio']<1 for r in v),top_gains=sorted(v,key=lambda r:r['ratio'],reverse=True)[:6],top_losses=sorted(v,key=lambda r:r['ratio'])[:6],note='Different selected plans and problem semantics; not hardware-only causal effect.'))
paired=[]
reported={r['slot'] for r in load(F/'component_p2_seed17/progress.json')['slots'] if r.get('search_completed') and r.get('feasible')}
for file in sorted((F/'component_p2_seed17/slots').glob('case_*/p*_n*/attempt_*/summary.json')):
 s=load(file)
 if not s.get('completed') or not s.get('best'):continue
 c,n=s['case'],s['num_cores']
 if f'{c}_p2_n{n}' not in reported:continue
 t=s['best']['record']['metrics']['makespan'];r=ix[c,2,n]
 paired.append(dict(case=c,cores=n,component_time=t,full_time=r['makespan'],ratio=t/r['makespan'],reduction_pct=100*(1-r['makespan']/t)))
pair_summary=dict(count=len(paired),distinct_cases=len({r['case'] for r in paired}),wins=sum(r['ratio']>1 for r in paired),ties=sum(r['ratio']==1 for r in paired),losses=sum(r['ratio']<1 for r in paired),mean_ratio=mean([r['ratio'] for r in paired]),rows=paired,note='Incomplete, completion-order-biased paired subset, not full100 independent evidence. Same cap may have unequal actual calls.')
weak=[]
for p in (1,2,3):
 v=[r for r in rows if r['problem']==p and r['cores']==5]
 weak.append(dict(problem=p,slowest_speedups=sorted(v,key=lambda r:r['speedup'])[:12],largest_lb_gap=sorted(v,key=lambda r:r['lb_ratio'],reverse=True)[:12]))
res=dict(scope='formal_v2 seed17, all100 x P1/P2/P3 x N2..5; original official singlecore denominator',row_count=1200,aggregates=agg,core_monotonicity=monotonic,lower_core_inheritance_screen=core_diagnostic,cross_problem_selected_plan_comparisons=cross,partial_component_comparison=pair_summary,weak_instances=weak,sources=sources)
dump(O/'performance.json',res)
with (O/'per_configuration.csv').open('w',encoding='utf-8-sig',newline='') as f:
 w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
print(json.dumps({k:res[k] for k in ['row_count','aggregates']},ensure_ascii=False))
print('MONOTONIC',[(r['problem'],r['cores_transition'],r['worse']) for r in monotonic])
print('PAIR', {k:v for k,v in pair_summary.items() if k!='rows'})
