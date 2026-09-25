"""Conservative production routing; experimental P1 searches stay opt-in elsewhere."""
import argparse
import json
from pathlib import Path

from common_run import score
from frontier_solver import run


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--case',type=int,required=True)
    p.add_argument('--problem',type=int,choices=(1,2,3),required=True)
    p.add_argument('--cores',type=int,choices=range(1,6),default=5)
    p.add_argument('--budget',type=int,default=24);p.add_argument('--seconds',type=float,default=240)
    p.add_argument('--timeout',type=float,default=60);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    # Only P2/P3 have passed the paired 2--5 core integration panel. P1 uses
    # its mature integrated trajectory; improved library plans are separate.
    s=run(f'case_{a.case:03d}',a.problem,a.cores,a.out,a.budget,a.seconds,a.timeout,
          'mature' if a.problem==1 else 'frontier')
    print(json.dumps(dict(calls=s['logical_calls'],best=score(s['best_record']) if s['best_record'] else None)))
