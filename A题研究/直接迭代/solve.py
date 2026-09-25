#!/usr/bin/env python3
"""User entry: P1 adaptive from scratch, or P1/P2/P3 warm improvement, 1..5 cores."""
import argparse
from pathlib import Path
from common_run import *

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--case',type=int,required=True,choices=range(1,101));p.add_argument('--problem',type=int,required=True,choices=(1,2,3));p.add_argument('--out',type=Path,required=True)
 p.add_argument('--cores',type=int,choices=range(1,6),default=5)
 p.add_argument('--p1-refinement',choices=('partition','tasks','tensor'),default='partition',help='P1 warm search: structural partition, Task refinement, or tensor regions')
 p.add_argument('--p1-method',choices=('component','adaptive','hybrid','portfolio','portfolio_diverse'),default='adaptive',help='P1 from-scratch method; portfolio integrates tensor regions and task refinement in one budget')
 p.add_argument('--p3-refinement',choices=('legacy','read_order','joint'),default='legacy',help='P3 warm search: original cache neighbourhood, fixed-core read ordering, or a one-budget combination')
 p.add_argument('--seed',type=int,default=17,help='P1 from-scratch candidate seed')
 p.add_argument('--evaluation-timeout',type=float,help='P1 from-scratch per-call timeout; total --seconds budget still applies')
 p.add_argument('--fresh-evaluations',action='store_true',help='P1 from-scratch: evaluate in this new output directory, without historical result-cache reuse')
 p.add_argument('--task-merge-caps',type=lambda s:[int(x) for x in s.split(',')],help='P1 tasks mode: evaluate only the requested merge caps, e.g.8,16')
 p.add_argument('--incumbent-plan',type=Path,help='Official-evaluate this plan first; works without historical run directories')
 p.add_argument('--budget',type=int,default=8);p.add_argument('--seconds',type=float,default=180);p.add_argument('--from-scratch',action='store_true')
 a=p.parse_args();case=f'case_{a.case:03d}';out=a.out.resolve()
 if out==DATA or DATA in out.parents:p.error('Outputs must be outside official data.')
 if out.exists():p.error('Use a new output directory.')
 if a.budget<1 or a.seconds<=0:p.error('Budget and seconds must be positive.')
 if a.from_scratch and a.incumbent_plan:p.error('Choose from-scratch or incumbent-plan.')
 if a.p1_refinement!='partition' and (a.problem!=1 or a.from_scratch):p.error('Task/tensor refinement requires P1 with an incumbent.')
 if a.p1_method!='adaptive' and (a.problem!=1 or not a.from_scratch):p.error('p1-method requires P1 from-scratch mode.')
 if a.p3_refinement!='legacy' and (a.problem!=3 or a.from_scratch):p.error('p3-refinement requires P3 with an incumbent.')
 if a.seed!=17 and not a.from_scratch:p.error('seed is currently supported only by from-scratch mode.')
 if a.evaluation_timeout is not None and (not a.from_scratch or a.evaluation_timeout<=0):p.error('positive evaluation-timeout requires from-scratch mode.')
 if a.fresh_evaluations and not a.from_scratch:p.error('fresh-evaluations requires P1 from-scratch mode.')
 if a.task_merge_caps is not None and (a.p1_refinement!='tasks' or any(x<1 for x in a.task_merge_caps)):p.error('positive task-merge-caps require tasks mode.')
 if a.from_scratch:
  if a.problem!=1:p.error('This new from-scratch entry currently implements P1; P2/P3 use the existing staged solver.')
  from p1_adaptive import run
  s=run(case,a.cores,a.p1_method,out,a.budget,a.seconds,seed=a.seed,evaluation_timeout=a.evaluation_timeout,
        evaluation_dir=out/'evaluations' if a.fresh_evaluations else None);record=s['best_record']
 else:
  b={} if a.incumbent_plan else known();initial=None;search_out=out;budget=a.budget;seconds=a.seconds;started=time.monotonic()
  if a.incumbent_plan:
   initial=run_candidate(case,a.problem,a.cores,read_json(a.incumbent_plan),out/'initial_evaluation',timeout=seconds)
   if initial['status']!='success':raise RuntimeError('Initial official evaluation failed: '+initial['status'])
   b[case,a.problem,a.cores]=initial;search_out=out/'search';budget-=1;seconds-=time.monotonic()-started
  if (case,a.problem,a.cores) not in b:p.error('No historical incumbent; pass --incumbent-plan or use --from-scratch for P1.')
  if budget<=0 or seconds<=0:
   record=initial;s={'best_record':record,'logical_calls':1,'new_calls':int(not record['cache_hit']),'elapsed_seconds':time.monotonic()-started}
   atomic_json(out/'best.plan.json',read_json(record['plan_path']));atomic_json(out/'summary.json',s)
   print(json.dumps({'output':str(out),'makespan':score(record)[0],'logical_calls':1,'new_calls':s['new_calls'],'elapsed_seconds':s['elapsed_seconds']}));return
  if a.problem==1:
   if a.p1_refinement=='tensor':
    from run_tensor_panel import run
    s=run(case,a.cores,b[case,1,a.cores],search_out,budget,seconds)
   else:
    if a.p1_refinement=='tasks':
     from run_task_panel import run
     s=run(case,b[case,1,a.cores],search_out,budget,seconds,cores=a.cores,merge_caps=a.task_merge_caps)
    else:
     from run_p1_warm import run
     s=run(case,b[case,1,a.cores],search_out,budget,seconds,cores=a.cores)
   record=s['best_record']
  elif a.problem==3:
   if a.p3_refinement in ('read_order','joint'):
    if a.p3_refinement=='joint':
     from p3_joint import run
    else:
     from p3_read_order import run
    s=run(case,b[case,3,a.cores],search_out,budget,seconds,cores=a.cores)
   else:
    from run_p3_refine import run
    s=run(case,b[case,3,a.cores],b.get((case,2,a.cores)),search_out,budget,seconds,cores=a.cores)
   record=s['best_record']
  else:
   from trace_warm import run
   s=run(case,b[case,2,a.cores],search_out,budget,seconds,cores=a.cores);record=s['best_record']
  if initial:
   s={**s,'initial_record':initial,'logical_calls':s['logical_calls']+1,'new_calls':s['new_calls']+int(not initial['cache_hit']),'elapsed_seconds':time.monotonic()-started}
   atomic_json(out/'best.plan.json',read_json(record['plan_path']));atomic_json(out/'summary.json',s)
 print(json.dumps({'output':str(out),'makespan':score(record)[0] if record else None,'logical_calls':s['logical_calls'],'new_calls':s['new_calls'],'elapsed_seconds':s['elapsed_seconds']},ensure_ascii=False))
if __name__=='__main__':main()
