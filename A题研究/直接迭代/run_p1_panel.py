from concurrent.futures import ProcessPoolExecutor,as_completed
import argparse
from common_run import *
from p1_adaptive import run

def main(a):
 out=Path(a.out).resolve();out.mkdir(exist_ok=False,parents=True);started=time.monotonic()
 cases=list(dict.fromkeys(int(x) for x in a.cases.split(',')))
 rows=[];done=[]
 with ProcessPoolExecutor(max_workers=a.workers) as ex:
  fs={ex.submit(run,f'case_{c:03d}',5,v,out/f'case_{c:03d}'/v,a.budget,a.seconds): (c,v) for c in cases for v in a.variants.split(',')}
  for f in as_completed(fs):
   s=f.result();c,v=fs[f];case=f'case_{c:03d}'
   old=read_json(R/f'advanced_solver/runs/formal_v2/full_p1_seed17/slots/{case}/p1_n5/attempt_0001/summary.json')
   successes=[x['record'] for x in old['evaluations'][:a.budget] if x['record']['status']=='success'];ref=min(successes,key=score);nr=s['best']['record'] if s['best'] else None
   row={'case':case,'variant':v,'old_prefix_time':score(ref)[0],'old_full_time':score(old['best']['record'])[0], 'new_time':score(nr)[0] if nr else None,
    'reduction_vs_prefix_pct':100*(1-score(nr)[0]/score(ref)[0]) if nr else None,'logical_calls':s['logical_calls'],'new_calls':s['new_calls'],
    'elapsed_seconds':s['elapsed_seconds'],'pruned':len(s['skipped']),'stop_reason':s['stop_reason']}
   rows.append(row);done.append({'case':case,'variant':v,'summary_path':str(out/case/v/'summary.json')});write_csv(out/'results.csv',rows)
   atomic_json(out/'progress.json',{'completed':len(rows),'expected':len(fs),'wall_seconds':time.monotonic()-started,'rows':rows});print(json.dumps(row),flush=True)
 summary={'complete':True,'cases':cases,'variants':a.variants.split(','),'cap':a.budget,'wall_seconds':time.monotonic()-started,'rows':rows,'runs':done,
 'scope':'same logical cap; old prefix reused; shared exact evaluator cache; observed elapsed is not cold timing comparison'}
 atomic_json(out/'summary.json',summary)
 print('DONE',flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--cases',default='52,51,21,95,78,2,71,37,29,13,88,44,3,5,9');p.add_argument('--variants',default='eft_only,adaptive');p.add_argument('--budget',type=int,default=8);p.add_argument('--seconds',type=float,default=180);p.add_argument('--workers',type=int,default=2);main(p.parse_args())
