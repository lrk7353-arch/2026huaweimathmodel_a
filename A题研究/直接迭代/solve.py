#!/usr/bin/env python3
"""User entry: P1/P2/P3 from-scratch search or warm improvement, 1..5 cores."""
import argparse
from pathlib import Path
from common_run import *

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--case',type=int,required=True,choices=range(1,101));p.add_argument('--problem',type=int,required=True,choices=(1,2,3));p.add_argument('--out',type=Path,required=True)
 p.add_argument('--cores',type=int,choices=range(1,6),default=5)
 p.add_argument('--engine',choices=('legacy','v2'),default='legacy',help='v2: unified cold/warm structural search for P1/P2/P3')
 p.add_argument('--p1-refinement',choices=('partition','tasks','tensor','region_joint','task_iterative'),default='partition',help='P1 warm search: partition/tasks/tensor, experimental region_joint, or accepted-parent task_iterative')
 p.add_argument('--p1-method',choices=('component','adaptive','hybrid','portfolio','portfolio_diverse','integrated','local_legacy'),default='adaptive',help='P1 from-scratch method; integrated adds joint Task refinement under the same budget')
 p.add_argument('--p23-method',choices=('component_wcc','staged','routed','trace_routed','integrated','local_legacy','wide_legacy','budget_greedy','budget_beam'),help='P2/P3 from scratch (default: trace_routed); budget variants allocate calls by observed gain/cost')
 p.add_argument('--p3-refinement',choices=('legacy','read_order','joint','interleave','feedback'),default='legacy',help='P3 warm search: original cache neighbourhood, fixed-core read ordering, or shared-budget controllers')
 p.add_argument('--seed',type=int,default=17,help='Candidate seed for P1 from-scratch or P3 interleave/feedback; older P3 methods require 17')
 p.add_argument('--evaluation-timeout',type=float,help='From-scratch per-call timeout; total --seconds budget still applies')
 p.add_argument('--fresh-evaluations',action='store_true',help='From-scratch or P3 warm search: use a new candidate evaluation directory without historical result-cache reuse')
 p.add_argument('--task-merge-caps',type=lambda s:[int(x) for x in s.split(',')],help='P1 tasks mode: evaluate only the requested merge caps, e.g.8,16')
 p.add_argument('--incumbent-plan',type=Path,help='Official-evaluate this plan first; works without historical run directories')
 p.add_argument('--budget',type=int,help='Logical calls; default 12 for P2/P3 from scratch, otherwise 8');p.add_argument('--seconds',type=float,help='Soft time budget; default 120 for P2/P3 from scratch, otherwise 180');p.add_argument('--from-scratch',action='store_true')
 a=p.parse_args();case=f'case_{a.case:03d}';out=a.out.resolve()
 p23_cold=a.from_scratch and a.problem in (2,3)
 if a.budget is None:a.budget=12 if p23_cold else 8
 if a.seconds is None:a.seconds=120 if p23_cold else 180
 if out==DATA or DATA in out.parents:p.error('Outputs must be outside official data.')
 if out.exists():p.error('Use a new output directory.')
 if a.budget<1 or a.seconds<=0:p.error('Budget and seconds must be positive.')
 if a.from_scratch and a.incumbent_plan:p.error('Choose from-scratch or incumbent-plan.')
 if a.engine=='v2':
  from unified_solver import solve_unified
  s=solve_unified(DATA/(case+'.json'),a.problem,a.cores,out,seconds=a.seconds,call_budget=a.budget,
      seed=a.seed,incumbent=a.incumbent_plan,evaluation_timeout=a.evaluation_timeout or 60)
  print(json.dumps({'output':str(out),'makespan':score(s['best_record'])[0] if s['best_record'] else None,
      'logical_calls':s['logical_calls'],'new_calls':s['new_calls'],'elapsed_seconds':s['elapsed_seconds']},ensure_ascii=False))
  return
 if a.p1_refinement!='partition' and (a.problem!=1 or a.from_scratch):p.error('Task/tensor refinement requires P1 with an incumbent.')
 if a.p1_method!='adaptive' and (a.problem!=1 or not a.from_scratch):p.error('p1-method requires P1 from-scratch mode.')
 if a.p23_method is not None and not p23_cold:p.error('p23-method requires P2/P3 from-scratch mode.')
 if a.p3_refinement!='legacy' and a.problem!=3:p.error('p3-refinement requires P3.')
 if a.from_scratch and a.problem==3 and a.p3_refinement not in ('legacy','feedback'):p.error('P3 from-scratch currently supports legacy or feedback refinement.')
 if a.from_scratch and a.problem in (2,3) and a.seed!=17:p.error('The experimental P2/P3 pipeline currently requires seed 17.')
 if a.seed!=17 and not (a.from_scratch or (a.problem==3 and a.p3_refinement in ('interleave','feedback'))):p.error('Non-17 seeds require P1 from-scratch or P3 interleave/feedback; older warm methods use fixed seed 17.')
 if a.evaluation_timeout is not None and (not a.from_scratch or a.evaluation_timeout<=0):p.error('positive evaluation-timeout requires from-scratch mode.')
 if a.fresh_evaluations and not (a.from_scratch or a.problem==3):p.error('fresh-evaluations requires from-scratch or P3 warm mode.')
 if a.task_merge_caps is not None and (a.p1_refinement!='tasks' or any(x<1 for x in a.task_merge_caps)):p.error('positive task-merge-caps require tasks mode.')
 if a.from_scratch:
  if a.problem==1:
   from p1_adaptive import run
   s=run(case,a.cores,a.p1_method,out,a.budget,a.seconds,seed=a.seed,evaluation_timeout=a.evaluation_timeout,
         evaluation_dir=out/'evaluations' if a.fresh_evaluations else None)
  else:
   from p23_pipeline import run
   s=run(case,a.problem,a.cores,a.p23_method or 'trace_routed',out,a.budget,a.seconds,seed=a.seed,
         evaluation_timeout=a.evaluation_timeout or 60,evaluation_dir=out/'evaluations' if a.fresh_evaluations else None,
         p3_refinement=a.p3_refinement)
  record=s['best_record']
 else:
  b={} if a.incumbent_plan else known();initial=None;search_out=out;budget=a.budget;seconds=a.seconds;started=time.monotonic()
  if a.incumbent_plan:
   initial=run_candidate(case,a.problem,a.cores,read_json(a.incumbent_plan),out/'initial_evaluation',timeout=seconds)
   if initial['status']!='success':raise RuntimeError('Initial official evaluation failed: '+initial['status'])
   b[case,a.problem,a.cores]=initial;search_out=out/'search';budget-=1;seconds-=time.monotonic()-started
  if (case,a.problem,a.cores) not in b:p.error('No historical incumbent; pass --incumbent-plan or use --from-scratch.')
  if budget<=0 or seconds<=0:
   record=initial;s={'best_record':record,'logical_calls':1,'new_calls':int(not record['cache_hit']),'elapsed_seconds':time.monotonic()-started}
   atomic_json(out/'best.plan.json',read_json(record['plan_path']));atomic_json(out/'summary.json',s)
   print(json.dumps({'output':str(out),'makespan':score(record)[0],'logical_calls':1,'new_calls':s['new_calls'],'elapsed_seconds':s['elapsed_seconds']}));return
  if a.problem==1:
   if a.p1_refinement=='task_iterative':
    from p1_iterative_tasks import run
    s=run(case,b[case,1,a.cores],search_out,budget,seconds,cores=a.cores)
   elif a.p1_refinement=='region_joint':
    from run_region_refine import run
    s=run(case,b[case,1,a.cores],search_out,budget,seconds,cores=a.cores)
   elif a.p1_refinement=='tensor':
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
   evaluation_dir=out/'evaluations' if a.fresh_evaluations else None
   if a.p3_refinement in ('interleave','feedback'):
    from p3_feedback import run
    s=run(case,b[case,3,a.cores],search_out,budget,seconds,cores=a.cores,policy=a.p3_refinement,evaluation_dir=evaluation_dir,seed=a.seed)
   elif a.p3_refinement in ('read_order','joint'):
    if a.p3_refinement=='joint':
     from p3_joint import run
    else:
     from p3_read_order import run
    s=run(case,b[case,3,a.cores],search_out,budget,seconds,cores=a.cores,evaluation_dir=evaluation_dir)
   else:
    from run_p3_refine import run
    s=run(case,b[case,3,a.cores],b.get((case,2,a.cores)),search_out,budget,seconds,cores=a.cores,evaluation_dir=evaluation_dir)
   record=s['best_record']
  else:
   from trace_warm import run
   s=run(case,b[case,2,a.cores],search_out,budget,seconds,cores=a.cores);record=s['best_record']
  if initial:
   s={**s,'initial_record':initial,'logical_calls':s['logical_calls']+1,'new_calls':s['new_calls']+int(not initial['cache_hit']),'elapsed_seconds':time.monotonic()-started}
   atomic_json(out/'best.plan.json',read_json(record['plan_path']));atomic_json(out/'summary.json',s)
 print(json.dumps({'output':str(out),'makespan':score(record)[0] if record else None,'logical_calls':s['logical_calls'],'new_calls':s['new_calls'],'elapsed_seconds':s['elapsed_seconds']},ensure_ascii=False))
if __name__=='__main__':main()
