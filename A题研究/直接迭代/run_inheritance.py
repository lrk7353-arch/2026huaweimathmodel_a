import argparse,copy,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from common_run import *
from core_inheritance import pad_plan

def main(out):
 out=Path(out).resolve();out.mkdir(exist_ok=False,parents=True);started=time.monotonic();best=known()
 original=read_json(R/'实验记录/主批次完成分析_20260924/performance.json')
 formal={(c['case'],g['problem'],5) for g in original['lower_core_inheritance_screen'] for c in g['cases']}
 proposals=[];skips=[]
 for k,t in sorted(best.items()):
  c,p,n=k
  if n!=5:continue
  lows=[r for (cc,pp,nn),r in best.items() if cc==c and pp==p and nn<n]
  s=min(lows,key=score) if lows else None
  if s is None or score(s)>=score(t):
   if k in formal:skips.append({'case':c,'problem':p,'reason':'current_best_already_at_least_as_good','current':score(t)[0]})
   continue
  proposals.append({'case':c,'problem':p,'cores':n,'source_cores':key(s)[2],'before':score(t)[0],'before_added':score(t)[1],
   'source_time':score(s)[0],'source_record':s,'old_record':t,'from_formal54':k in formal})
 priority={44:0,46:1,69:2,71:3,96:4}
 proposals.sort(key=lambda x:(priority.get(int(x['case'][-3:]),9),-(1-x['source_time']/x['before'])))
 atomic_json(out/'plan.json',{'proposals':proposals,'formal_screen_count':len(formal),'formal_skipped':skips,'workers':2})
 print(json.dumps({'proposals':len(proposals),'formal_skipped':len(skips)},ensure_ascii=False),flush=True)
 rows=[];results=[]
 def one(x):
  d=out/x['case']/f'p{x["problem"]}_n5';d.mkdir(parents=True)
  plan=pad_plan(read_json(x['source_record']['plan_path']),5)
  record=run_candidate(x['case'],x['problem'],5,plan,out/'evaluations')
  ok=record['status']=='success';accepted=ok and score(record)<score(x['old_record'])
  result={**x,'record':record,'accepted':accepted};atomic_json(d/'result.json',result)
  if accepted:atomic_json(d/'best.plan.json',plan)
  return result
 with ThreadPoolExecutor(max_workers=2) as ex:
  jobs=[ex.submit(one,x) for x in proposals]
  for fut in as_completed(jobs):
   x=fut.result();results.append(x);r=x['record'];after=score(r)[0] if r['status']=='success' else None
   row={k:x[k] for k in ('case','problem','cores','source_cores','before','source_time')}
   row.update(after=after,status=r['status'],accepted=x['accepted'],reduction_pct=100*(1-after/x['before']) if after else None,elapsed_seconds=r['elapsed_seconds'],cache_hit=r['cache_hit'])
   rows.append(row);write_csv(out/'results.csv',rows)
   atomic_json(out/'progress.json',{'completed':len(results),'expected':len(proposals),'improved':sum(z['accepted'] for z in results),'elapsed_seconds':time.monotonic()-started})
   print(json.dumps(row,ensure_ascii=False),flush=True)
 atomic_json(out/'summary.json',{'complete':True,'proposals':len(proposals),'formal_skipped':skips,'improved':sum(x['accepted'] for x in results),
 'success':sum(x['record']['status']=='success' for x in results),'logical_calls':len(results),'new_calls':sum(not x['record']['cache_hit'] for x in results),'elapsed_seconds':time.monotonic()-started,'results':results})
 print('DONE',flush=True)
if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--out',required=True);main(a.parse_args().out)
