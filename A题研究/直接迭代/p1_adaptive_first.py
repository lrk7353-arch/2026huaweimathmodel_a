"""P1 bounded search: WCC controls, optional EFT-only or selective refinement."""
import argparse,time
from common_run import *
from advanced_solver.component_baseline import generate_component_candidates
from advanced_solver.engine import generate_coarse_p1_candidates
from p1_selective import generate_selective_candidates,task_lower_bound

def plan_key(plan):
 # Exact serialization, including insertion order; used only for local dedup.
 return json.dumps(plan,ensure_ascii=False,separators=(',',':'))

def run(case,cores,variant,out,budget=8,time_budget=180,seed=17):
 out=Path(out);out.mkdir(parents=True,exist_ok=False);started=time.monotonic()
 ir=GraphIR.from_path(DATA/(case+'.json'));calls=[];skipped=[];best=None;seen=set();deadline=started+time_budget
 component,diag=generate_component_candidates(ir,cores,max_candidates=4,seed=seed)
 def apply(candidates):
  nonlocal best
  for c in candidates:
   if len(calls)>=budget or time.monotonic()>=deadline:break
   signature=plan_key(c['plan'])
   if signature in seen:continue
   seen.add(signature)
   bound=task_lower_bound(ir,c['plan'])['value'] if variant=='adaptive' else None
   if best and bound is not None and bound>score(best['record'])[0]:
    skipped.append({'name':c['name'],'reason':'necessary_task_bound','bound':bound});continue
   remaining=deadline-time.monotonic()
   if remaining<=0:break
   validate_plan(ir,c['plan'])
   rec=evaluate(DATA/(case+'.json'),c['plan'],1,R/'advanced_solver/runs/formal_v2/evaluations',timeout=min(180 if len(ir.compute_ids)>10000 else 60,remaining),config_path=DATA/'config.txt')
   if rec['status']=='success' and bound is not None and bound>score(rec)[0]:raise AssertionError('bound exceeds actual result')
   accepted=rec['status']=='success' and (best is None or score(rec)<score(best['record']))
   calls.append({'name':c['name'],'record':rec,'accepted':accepted,'bound':bound})
   if accepted:
    best={'name':c['name'],'record':rec};atomic_json(out/'best.plan.json',c['plan'])
   atomic_json(out/'progress.json',{'case':case,'variant':variant,'calls':len(calls),'best':best})
 apply(component)
 if len(calls)<budget and time.monotonic()<deadline:
  if variant=='eft_only':
   extra,extra_diag=generate_coarse_p1_candidates(ir,cores,12,seed)
   extra=[c for c in extra if c['metadata']['assignment']=='p1_eft']
  elif variant=='adaptive':
   extra,extra_diag=generate_selective_candidates(ir,cores,max_candidates=24,seed=seed)
  else:raise ValueError(variant)
  apply(extra)
 else:extra_diag={}
 summary={'case':case,'problem':1,'num_cores':cores,'variant':variant,'budget':budget,'best':best,'evaluations':calls,'skipped':skipped,
  'elapsed_seconds':time.monotonic()-started,'logical_calls':len(calls),'new_calls':sum(not c['record']['cache_hit'] for c in calls),
  'candidate_generation':extra_diag,'stop_reason':'time_budget' if time.monotonic()>=deadline else 'budget_or_pool_exhausted','soft_time_budget':time_budget}
 atomic_json(out/'summary.json',summary);return summary

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--case',type=int,required=True);p.add_argument('--cores',type=int,default=5);p.add_argument('--variant',choices=['eft_only','adaptive'],default='adaptive');p.add_argument('--out',required=True);p.add_argument('--budget',type=int,default=8);p.add_argument('--seconds',type=float,default=180)
 a=p.parse_args();s=run(f'case_{a.case:03d}',a.cores,a.variant,a.out,a.budget,a.seconds);print(json.dumps({k:v for k,v in s.items() if k not in ('evaluations','candidate_generation','best','skipped')},ensure_ascii=False))
