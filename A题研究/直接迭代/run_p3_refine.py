import argparse,gzip,time
from concurrent.futures import ProcessPoolExecutor,as_completed
from common_run import *
from advanced_solver.cache_refine import generate_cache_candidates

def exact(p):return json.dumps(p,ensure_ascii=False,separators=(',',':'))
def run(case,old,p2,out,budget=8,seconds=180,cores=5):
 if key(old)!=(case,3,cores) or (p2 is not None and key(p2)!=(case,2,cores)):raise ValueError('Incumbent case/problem/core mismatch')
 out=Path(out);out.mkdir(exist_ok=False,parents=True);start=time.monotonic();deadline=start+seconds
 ir=GraphIR.from_path(DATA/(case+'.json'));best=old;plan=read_json(old['plan_path']);seen={exact(plan)};calls=[];phases=[]
 def apply(c,phase):
  nonlocal best,plan
  sig=exact(c['plan'])
  if sig in seen or len(calls)>=budget or time.monotonic()>=deadline:return
  seen.add(sig);validate_plan(ir,c['plan'])
  rec=evaluate(DATA/(case+'.json'),c['plan'],3,R/'advanced_solver/runs/formal_v2/evaluations',timeout=min(180 if len(ir.compute_ids)>10000 else 60,max(.1,deadline-time.monotonic())),config_path=DATA/'config.txt')
  ok=rec['status']=='success' and score(rec)<score(best)
  calls.append({'name':c['name'],'phase':phase,'record':rec,'accepted':ok})
  if ok:best=rec;plan=c['plan'];atomic_json(out/'best.plan.json',plan)
  atomic_json(out/'progress.json',{'case':case,'before':score(old)[0],'best_time':score(best)[0],'calls':len(calls)})
 if p2 is not None:apply({'name':'inherit_P2_plan','plan':read_json(p2['plan_path'])},'cross_problem')
 phases.append({'phase':'after_P2','makespan':score(best)[0],'P2_source_available':p2 is not None})
 for rd in range(2):
  if len(calls)>=budget or time.monotonic()>=deadline:break
  with gzip.open(best['result_path'],'rt') as f:raw=json.load(f)
  cs,diag=generate_cache_candidates(ir,plan,raw,num_cores=cores,max_candidates=12,round_index=rd,seed=17)
  controls=[c for c in cs if c['metadata']['is_reencoding_control']]
  others=[c for c in cs if not c['metadata']['is_reencoding_control']]
  before=score(best)[0]
  for c in controls:apply(c,'schedule_control')
  ctrl=score(best)[0]
  # Diversify mechanisms instead of spending a whole round on one target.
  ordered=[];groups={}
  for c in others:groups.setdefault(c['metadata']['mechanism'],[]).append(c)
  while any(groups.values()):
   for g in groups.values():
    if g:ordered.append(g.pop(0))
  cap=3 if rd==0 else budget-len(calls)
  before_calls=len(calls)
  for c in ordered:
   if len(calls)-before_calls>=cap:break
   apply(c,'cache_guided')
  phases.append({'round':rd,'before':before,'after_control':ctrl,'after_guided':score(best)[0]})
 atomic_json(out/'best.plan.json',plan)
 s={'case':case,'problem':3,'num_cores':cores,'before':score(old)[0],'after':score(best)[0], 'best_record':best,'logical_calls':len(calls),
  'new_calls':sum(not c['record']['cache_hit'] for c in calls),'calls':calls,'phases':phases,'elapsed_seconds':time.monotonic()-start,
  'stop_reason':'time_budget' if time.monotonic()>=deadline else 'budget_or_candidates','scope':'extra-budget refinement of current portfolio; no from-scratch fairness claim'}
 atomic_json(out/'summary.json',s);return s

def main(a):
 out=Path(a.out).resolve();out.mkdir(exist_ok=False,parents=True);t=time.monotonic();b=known();rows=[]
 cases=[f'case_{int(x):03d}' for x in a.cases.split(',')]
 with ProcessPoolExecutor(max_workers=2) as ex:
  fs=[ex.submit(run,c,b[c,3,5],b[c,2,5],out/c,a.budget,a.seconds) for c in cases]
  for f in as_completed(fs):
   s=f.result();row={k:s[k] for k in ['case','before','after','logical_calls','new_calls','elapsed_seconds','stop_reason']};row['reduction_pct']=100*(1-s['after']/s['before']);rows.append(row)
   write_csv(out/'results.csv',rows);atomic_json(out/'progress.json',{'completed':len(rows),'expected':len(cases),'rows':rows});print(json.dumps(row),flush=True)
 atomic_json(out/'summary.json',{'complete':True,'rows':rows,'wall_seconds':time.monotonic()-t,'cases':cases,'budget':a.budget})
 print('DONE',flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--cases',default='49,6,5,3,65,46,44,71,69,93');p.add_argument('--budget',type=int,default=8);p.add_argument('--seconds',type=float,default=180);main(p.parse_args())
