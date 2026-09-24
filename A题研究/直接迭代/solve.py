#!/usr/bin/env python3
"""User entry: P1 adaptive from scratch, or P1/P3 warm improvement."""
import argparse
from pathlib import Path
from common_run import *

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--case',type=int,required=True,choices=range(1,101));p.add_argument('--problem',type=int,required=True,choices=(1,2,3));p.add_argument('--out',type=Path,required=True)
 p.add_argument('--budget',type=int,default=8);p.add_argument('--seconds',type=float,default=180);p.add_argument('--from-scratch',action='store_true')
 a=p.parse_args();case=f'case_{a.case:03d}';out=a.out.resolve()
 if out==DATA or DATA in out.parents:p.error('Outputs must be outside official data.')
 if out.exists():p.error('Use a new output directory.')
 if a.budget<1 or a.seconds<=0:p.error('Budget and seconds must be positive.')
 if a.from_scratch:
  if a.problem!=1:p.error('This new from-scratch entry currently implements P1; P2/P3 use the existing staged solver.')
  from p1_adaptive import run
  s=run(case,5,'adaptive',out,a.budget,a.seconds);record=s['best']['record'] if s['best'] else None
 else:
  b=known()
  if a.problem==1:
   from run_p1_warm import run
   s=run(case,b[case,1,5],out,a.budget,a.seconds);record=s['best']['record']
  elif a.problem==3:
   from run_p3_refine import run
   s=run(case,b[case,3,5],b[case,2,5],out,a.budget,a.seconds);record=s['best_record']
  else:
   from trace_warm import run
   s=run(case,b[case,2,5],out,a.budget,a.seconds);record=s['best_record']
 print(json.dumps({'output':str(out),'makespan':score(record)[0] if record else None,'logical_calls':s['logical_calls'],'new_calls':s['new_calls'],'elapsed_seconds':s['elapsed_seconds']},ensure_ascii=False))
if __name__=='__main__':main()
