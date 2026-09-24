"""Bounded P2 trace refinement of an existing official plan."""
import gzip,time
from common_run import *
from advanced_solver.trace_refine import generate_trace_candidates

def run(case,old,out,budget=8,seconds=120):
 out=Path(out);out.mkdir(exist_ok=False,parents=True);t=time.monotonic();deadline=t+seconds;ir=GraphIR.from_path(DATA/(case+'.json'));best=old;plan=read_json(old['plan_path']);calls=[]
 def exact(p):return json.dumps(p,ensure_ascii=False,separators=(',',':'))
 seen={exact(plan)}
 for rd in range(3):
  if len(calls)>=budget or time.monotonic()>=deadline:break
  with gzip.open(best['result_path'],'rt') as f:raw=json.load(f)
  cs,_=generate_trace_candidates(ir,plan,raw,num_cores=5,max_candidates=8,round_index=rd,seed=17)
  before=len(calls)
  for c in cs:
   if len(calls)>=budget or len(calls)-before>=4 or time.monotonic()>=deadline:break
   sig=exact(c['plan'])
   if sig in seen:continue
   seen.add(sig);validate_plan(ir,c['plan'])
   r=evaluate(DATA/(case+'.json'),c['plan'],2,R/'advanced_solver/runs/formal_v2/evaluations',timeout=min(60,max(.1,deadline-time.monotonic())),config_path=DATA/'config.txt')
   ok=r['status']=='success' and score(r)<score(best);calls.append({'name':c['name'],'record':r,'accepted':ok})
   if ok:best=r;plan=c['plan']
 atomic_json(out/'best.plan.json',plan)
 s={'case':case,'problem':2,'num_cores':5,'before':score(old)[0],'after':score(best)[0],'best_record':best,'calls':calls,'logical_calls':len(calls),'new_calls':sum(not c['record']['cache_hit'] for c in calls),'elapsed_seconds':time.monotonic()-t}
 atomic_json(out/'summary.json',s);return s
