"""Short P1 improvement pass retaining an existing official feasible plan."""
import argparse,time
from concurrent.futures import ProcessPoolExecutor,as_completed
from common_run import *
from p1_selective import generate_selective_candidates,task_lower_bound
from advanced_solver.engine import generate_coarse_p1_candidates

def exact(p):return json.dumps(p,ensure_ascii=False,separators=(',',':'))
def run(case,old,out,budget,seconds,cores=5):
 if key(old)!=(case,1,cores):raise ValueError('Incumbent case/problem/core mismatch')
 out=Path(out);out.mkdir(parents=True,exist_ok=False);t=time.monotonic();deadline=t+seconds;ir=GraphIR.from_path(DATA/(case+'.json'));best=old;plan=read_json(old['plan_path']);seen={exact(plan)};calls=[];skips=[]
 cs,diag=generate_selective_candidates(ir,cores,max_candidates=24,seed=17)
 coarse,_=generate_coarse_p1_candidates(ir,cores,12,17);eft=[c for c in coarse if c['metadata']['assignment']=='p1_eft']
 cs=cs+eft if diag['heavy_component_ids'] else eft[:2]+cs[:1]+eft[2:]+cs[1:]
 for c in cs:
  if len(calls)>=budget or time.monotonic()>=deadline:break
  sig=exact(c['plan'])
  if sig in seen:continue
  seen.add(sig);bound=task_lower_bound(ir,c['plan'])['value']
  if bound>score(best)[0]:skips.append({'name':c['name'],'bound':bound});continue
  rec=evaluate(DATA/(case+'.json'),c['plan'],1,R/'advanced_solver/runs/formal_v2/evaluations',timeout=min(180 if len(ir.compute_ids)>10000 else 60,max(.1,deadline-time.monotonic())),config_path=DATA/'config.txt')
  ok=rec['status']=='success' and score(rec)<score(best);calls.append({'name':c['name'],'record':rec,'accepted':ok})
  if rec['status']=='success' and bound>score(rec)[0]:raise AssertionError('bad lower bound')
  if ok:best=rec;plan=c['plan']
  atomic_json(out/'progress.json',{'case':case,'calls':len(calls),'before':score(old)[0],'after':score(best)[0]})
 atomic_json(out/'best.plan.json',plan)
 s={'case':case,'problem':1,'num_cores':cores,'before':score(old)[0],'after':score(best)[0],'best':{'record':best},'best_record':best,'evaluations':calls,'logical_calls':len(calls),'new_calls':sum(not x['record']['cache_hit'] for x in calls),
    'elapsed_seconds':time.monotonic()-t,'pruned':len(skips),'stop_reason':'time_budget' if time.monotonic()>=deadline else 'budget_or_pool','scope':'extra budget warm search; incumbent retained on timeout'}
 atomic_json(out/'summary.json',s);return s

def main(a):
 out=Path(a.out).resolve();out.mkdir(parents=True,exist_ok=False);t=time.monotonic();best=known();tested=set()
 for name in ['P1开发_v2','P1扩展_v2','P1大图_v2']:
  for p in (R/'直接迭代/运行结果'/name).glob('case_*/adaptive/summary.json'):
   s=read_json(p)
   if s.get('best'):tested.add(s['case'])
 cases=[f'case_{i:03d}' for i in range(1,101) if f'case_{i:03d}' not in tested]
 atomic_json(out/'plan.json',{'cases':cases,'already_tested':sorted(tested),'extra_call_cap':a.budget,'seconds_per_case':a.seconds,'workers':2})
 print(json.dumps({'remaining_cases':len(cases)}),flush=True);rows=[]
 with ProcessPoolExecutor(max_workers=2) as ex:
  fs=[ex.submit(run,c,best[c,1,5],out/c/'warm',a.budget,a.seconds) for c in cases]
  for f in as_completed(fs):
   s=f.result();row={k:s[k] for k in ('case','before','after','logical_calls','new_calls','elapsed_seconds','pruned','stop_reason')};row['reduction_pct']=100*(1-s['after']/s['before']);rows.append(row)
   write_csv(out/'results.csv',rows);atomic_json(out/'progress.json',{'completed':len(rows),'expected':len(cases),'rows':rows,'wall_seconds':time.monotonic()-t});print(json.dumps(row),flush=True)
 atomic_json(out/'summary.json',{'complete':True,'rows':rows,'cases':cases,'wall_seconds':time.monotonic()-t,'scope':'warm extension; combined with earlier cold panels is not uniform-budget algorithm experiment'})
 print('DONE',flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--budget',type=int,default=4);p.add_argument('--seconds',type=float,default=90);main(p.parse_args())
