"""Complete the P2/P3 five-core cross evaluations using existing trial records."""
import argparse,gzip,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from common_run import *

def main(a):
 out=Path(a.out).resolve();out.mkdir(exist_ok=False,parents=True);t=time.monotonic();deadline=t+a.seconds;cases={};jobs=[];reused=0
 for i in range(1,101):
  c=f'case_{i:03d}';ss={p:read_json(R/f'advanced_solver/runs/formal_v2/full_p{p}_seed17/slots/{c}/p{p}_n5/attempt_0001/summary.json') for p in (2,3)}
  cells={'t2_pi2':ss[2]['best']['record'],'t3_pi3':ss[3]['best']['record']};cases[c]={'case':c,'cells':cells}
  for p,q in [(2,3),(3,2)]:
   label=f't{p}_pi{q}';target=ss[q]['best'];match=next((v['record'] for v in ss[p]['evaluations'] if v.get('plan_sha256')==target['plan_sha256'] and v['record']['status']=='success'),None)
   if match:
    # Already audited formal trial; use its recorded official score directly.
    cells[label]=match;reused+=1
   else:jobs.append((c,p,label,target['record']['plan_path']))
 atomic_json(out/'plan.json',{'formal_selected_plans_only':True,'existing_cross_cells':reused,'missing_cells':len(jobs),'workers':2})
 print(json.dumps({'reused_cross_cells':reused,'calls_needed':len(jobs)}),flush=True)
 def work(job):
  c,p,label,path=job
  if time.monotonic()>=deadline:return job,None
  plan=read_json(path);ir=GraphIR.from_path(DATA/(c+'.json'));validate_plan(ir,plan)
  rec=evaluate(DATA/(c+'.json'),plan,p,R/'advanced_solver/runs/formal_v2/evaluations',timeout=min(180 if len(ir.compute_ids)>10000 else 60,max(.1,deadline-time.monotonic())),config_path=DATA/'config.txt')
  atomic_json(out/c/(label+'.json'),rec);return job,rec
 records=[]
 with ThreadPoolExecutor(max_workers=2) as ex:
  for f in as_completed([ex.submit(work,j) for j in jobs]):
   (c,p,label,path),rec=f.result()
   if rec:
    records.append(rec)
    if rec['status']=='success':cases[c]['cells'][label]=rec
   atomic_json(out/'progress.json',{'returned':len(records),'expected':len(jobs),'complete_cases':sum(len(x['cells'])==4 for x in cases.values()),'wall_seconds':time.monotonic()-t})
   print(json.dumps({'case':c,'cell':label,'status':rec['status'] if rec else 'not_started'}),flush=True)
 rows=[]
 for c,x in cases.items():
  row={'case':c,'complete':len(x['cells'])==4}
  row.update({k:score(v)[0] for k,v in x['cells'].items()})
  if row['complete']:
   row.update(hardware_ratio_at_P2_plan=row['t2_pi2']/row['t3_pi2'],policy_ratio_under_P3=row['t3_pi2']/row['t3_pi3'],total_ratio=row['t2_pi2']/row['t3_pi3'])
  rows.append(row)
 write_csv(out/'results.csv',rows)
 atomic_json(out/'summary.json',{'complete':all(x['complete'] for x in rows),'complete_cases':sum(x['complete'] for x in rows),'cases':list(cases.values()),'rows':rows,'logical_calls':len(records),'new_calls':sum(not r['cache_hit'] for r in records),'reused_cross_cells':reused,'wall_seconds':time.monotonic()-t})
 print('DONE',flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--seconds',type=float,default=1200);main(p.parse_args())
