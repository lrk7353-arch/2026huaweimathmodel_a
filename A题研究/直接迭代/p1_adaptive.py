"""P1 bounded search: WCC controls, optional EFT-only or selective refinement."""
import argparse,time,gzip
from common_run import *
from advanced_solver.component_baseline import generate_component_candidates
from advanced_solver.engine import generate_coarse_p1_candidates
from p1_selective import generate_selective_candidates,task_lower_bound

def plan_key(plan):
 # Exact serialization, including insertion order; used only for local dedup.
 return json.dumps(plan,ensure_ascii=False,separators=(',',':'))

def run(case,cores,variant,out,budget=8,time_budget=180,seed=17,evaluation_timeout=None,evaluation_dir=None):
 if variant in ('integrated','local_legacy'):
  if seed!=17:raise ValueError('integrated cold search currently requires seed 17')
  from cold_portfolio import run as integrated_run
  return integrated_run(case,1,cores,variant,out,budget,time_budget,evaluation_timeout or 25,evaluation_dir)
 if variant in ('portfolio','portfolio_diverse'):
  from p1_portfolio import run as portfolio_run
  return portfolio_run(case,cores,out,budget,time_budget,seed,evaluation_timeout,structural_diversity=variant=='portfolio_diverse',evaluation_dir=evaluation_dir)
 if variant not in ('component','legacy','eft_only','adaptive','hybrid'):raise ValueError(variant)
 if budget<1 or time_budget<=0:raise ValueError('positive budget and time required')
 if evaluation_timeout is not None and evaluation_timeout<=0:raise ValueError('positive evaluation timeout required')
 out=Path(out);out.mkdir(parents=True,exist_ok=False);started=time.monotonic()
 evaluation_dir=Path(evaluation_dir) if evaluation_dir is not None else R/'advanced_solver/runs/formal_v2/evaluations'
 ir=GraphIR.from_path(DATA/(case+'.json'));calls=[];skipped=[];best=None;seen=set();deadline=started+time_budget
 component,diag=generate_component_candidates(ir,cores,max_candidates=budget if variant=='component' else 4,seed=seed)
 reserve=min(4,budget//3) if variant=='hybrid' else 0
 prefix_cap=budget-reserve
 def apply(candidates,cap=budget,phase='partition'):
  nonlocal best
  for c in candidates:
   if len(calls)>=cap or time.monotonic()>=deadline:break
   signature=plan_key(c['plan'])
   if signature in seen:continue
   seen.add(signature)
   bound=(c['metadata']['lower_bound'] if phase=='task_refine' else task_lower_bound(ir,c['plan'])['value']) if variant in ('adaptive','hybrid') else None
   if best and bound is not None and bound>score(best['record'])[0]:
    skipped.append({'name':c['name'],'reason':'necessary_bound','bound':bound,'phase':phase});continue
   remaining=deadline-time.monotonic()
   if remaining<=0:break
   validate_plan(ir,c['plan'])
   per_call=evaluation_timeout if evaluation_timeout is not None else (180 if len(ir.compute_ids)>10000 else 60)
   rec=evaluate(DATA/(case+'.json'),c['plan'],1,evaluation_dir,timeout=min(per_call,remaining),config_path=DATA/'config.txt')
   if rec['status']=='success' and bound is not None and bound>score(rec)[0]:raise AssertionError('bound exceeds actual result')
   accepted=rec['status']=='success' and (best is None or score(rec)<score(best['record']))
   calls.append({'name':c['name'],'record':rec,'accepted':accepted,'bound':bound,'phase':phase})
   if accepted:
    best={'name':c['name'],'record':rec};atomic_json(out/'best.plan.json',c['plan'])
   atomic_json(out/'progress.json',{'case':case,'variant':variant,'calls':len(calls),'best':best})
 apply(component,prefix_cap)
 extra=[]
 if len(calls)<prefix_cap and time.monotonic()<deadline:
  if variant=='component':
   extra=[];extra_diag={'routing':'whole_component_only'}
  elif variant=='legacy':
   extra,extra_diag=generate_coarse_p1_candidates(ir,cores,12,seed)
  elif variant=='eft_only':
   extra,extra_diag=generate_coarse_p1_candidates(ir,cores,12,seed)
   extra=[c for c in extra if c['metadata']['assignment']=='p1_eft']
  elif variant in ('adaptive','hybrid'):
   extra,extra_diag=generate_selective_candidates(ir,cores,max_candidates=24,seed=seed)
   if not extra_diag['heavy_component_ids']:
    coarse,_=generate_coarse_p1_candidates(ir,cores,12,seed)
    eft=[c for c in coarse if c['metadata']['assignment']=='p1_eft']
    extra=eft[:2]+extra[:1]+eft[2:]+extra[1:]
    extra_diag['routing']='light_components_keep_coarse_EFT_and_whole_control'
   else:extra_diag['routing']='heavy_components_selective_split'
  else:raise ValueError(variant)
  apply(extra,prefix_cap)
 else:extra_diag={}
 prefix_calls=len(calls)
 if variant=='hybrid' and len(calls)<budget and time.monotonic()<deadline:
  if best:
   from p1_task_refine import generate
   with gzip.open(best['record']['result_path'],'rt') as f:raw=json.load(f)
   local=generate(ir,read_json(best['record']['plan_path']),raw,seed)
   apply(local,phase='task_refine')
  # If the local pool is exhausted/pruned, spend remaining calls on structural candidates.
  apply(component+extra,phase='partition_fallback')
 summary={'case':case,'problem':1,'num_cores':cores,'variant':variant,'budget':budget,'best':best,'evaluations':calls,'skipped':skipped,
  'elapsed_seconds':time.monotonic()-started,'logical_calls':len(calls),'new_calls':sum(not c['record']['cache_hit'] for c in calls),
  'candidate_generation':extra_diag,'stop_reason':'time_budget' if time.monotonic()>=deadline else 'budget_or_pool_exhausted','soft_time_budget':time_budget,
  'prefix_calls':prefix_calls,'task_call_reserve':reserve,'best_record':best['record'] if best else None,'evaluation_dir':str(evaluation_dir)}
 atomic_json(out/'summary.json',summary);return summary

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--case',type=int,required=True);p.add_argument('--cores',type=int,default=5);p.add_argument('--variant',choices=['component','legacy','eft_only','adaptive','hybrid','portfolio','portfolio_diverse'],default='adaptive');p.add_argument('--out',required=True);p.add_argument('--budget',type=int,default=8);p.add_argument('--seconds',type=float,default=180)
 a=p.parse_args();s=run(f'case_{a.case:03d}',a.cores,a.variant,a.out,a.budget,a.seconds);print(json.dumps({k:v for k,v in s.items() if k not in ('evaluations','candidate_generation','best','skipped')},ensure_ascii=False))
