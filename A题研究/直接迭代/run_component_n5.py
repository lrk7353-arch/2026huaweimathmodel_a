"""Fill only missing P2/N5 strong-component comparisons; bounded wall window."""
import argparse,time
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from common_run import *
from advanced_solver.component_baseline import generate_component_candidates

def one(case,out,deadline):
 out=Path(out)/case;out.mkdir(parents=True,exist_ok=False);t=time.monotonic();ir=GraphIR.from_path(DATA/(case+'.json'))
 cs,diag=generate_component_candidates(ir,5,max_candidates=24,seed=17);calls=[];best=None
 for c in cs:
  if time.monotonic()>=deadline:break
  validate_plan(ir,c['plan'])
  rec=evaluate(DATA/(case+'.json'),c['plan'],2,R/'advanced_solver/runs/formal_v2/evaluations',timeout=min(180 if len(ir.compute_ids)>10000 else 60,max(.1,deadline-time.monotonic())),config_path=DATA/'config.txt')
  calls.append({'name':c['name'],'record':rec})
  if rec['status']=='success' and (best is None or score(rec)<score(best['record'])):best={'name':c['name'],'record':rec};atomic_json(out/'best.plan.json',c['plan'])
  atomic_json(out/'progress.json',{'case':case,'evaluated':len(calls),'candidates':len(cs),'best':best})
 s={'case':case,'problem':2,'num_cores':5,'completed':len(calls)==len(cs),'evaluations':calls,'best':best,'elapsed_seconds':time.monotonic()-t,
    'logical_cap':24,'logical_calls':len(calls),'new_calls':sum(not x['record']['cache_hit'] for x in calls),'candidate_count':len(cs)}
 atomic_json(out/'summary.json',s);return s

def main(a):
 out=Path(a.out).resolve();out.mkdir(exist_ok=False,parents=True);t=time.monotonic();deadline=t+a.seconds;done={};rows=[]
 for p in (R/'advanced_solver/runs/formal_v2/component_p2_seed17/slots').glob('case_*/p2_n5/attempt_*/summary.json'):
  s=read_json(p)
  if s.get('completed') and s.get('best'):done[s['case']]=str(p)
 cases=[f'case_{c:03d}' for c in range(1,101) if f'case_{c:03d}' not in done]
 atomic_json(out/'plan.json',{'existing_complete':done,'new_cases':cases,'logical_cap':24,'workers':2,'wall_budget_seconds':a.seconds})
 print(json.dumps({'existing':len(done),'new_cases':len(cases)}),flush=True)
 pending=iter(cases);running={};completed=[]
 with ThreadPoolExecutor(max_workers=2) as ex:
  def submit():
   if time.monotonic()>=deadline:return
   c=next(pending,None)
   if c:running[ex.submit(one,c,out,deadline)]=c
  submit();submit()
  while running:
   finished,_=wait(running,return_when=FIRST_COMPLETED)
   for f in finished:
    c=running.pop(f);s=f.result();completed.append(s['case'])
    row={k:s[k] for k in ['case','completed','logical_calls','new_calls','elapsed_seconds']};row['makespan']=score(s['best']['record'])[0] if s['best'] else None;rows.append(row)
    write_csv(out/'results.csv',rows);atomic_json(out/'progress.json',{'completed':sum(x['completed'] for x in rows),'expected':len(cases),'existing':len(done),'wall_seconds':time.monotonic()-t,'rows':rows});print(json.dumps(row),flush=True);submit()
 remaining=[c for c in cases if c not in completed];atomic_json(out/'summary.json',{'complete':not remaining and all(x['completed'] for x in rows),'existing':done,'rows':rows,'remaining':remaining,'wall_seconds':time.monotonic()-t})
 print('DONE',flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--seconds',type=float,default=1200);main(p.parse_args())
