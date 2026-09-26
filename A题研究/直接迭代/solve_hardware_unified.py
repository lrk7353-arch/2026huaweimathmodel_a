"""Conservative production routing; experimental P1 searches stay opt-in elsewhere."""
import argparse
import json
from pathlib import Path

from common_run import score, atomic_json
from frontier_solver import run
from recover_empty_cold import recover


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--case',type=int,required=True)
    p.add_argument('--problem',type=int,choices=(1,2,3),required=True)
    p.add_argument('--cores',type=int,choices=range(1,6),default=5)
    p.add_argument('--budget',type=int,default=24);p.add_argument('--seconds',type=float,default=240)
    p.add_argument('--timeout',type=float,default=60);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--strict-time-limit',action='store_true',
        help='disable the documented empty-run recovery extension')
    a=p.parse_args()
    # Only P2/P3 have passed the paired 2--5 core integration panel. P1 uses
    # its mature integrated trajectory; improved library plans are separate.
    s=run(f'case_{a.case:03d}',a.problem,a.cores,a.out,a.budget,a.seconds,a.timeout,
          'mature' if a.problem==1 else 'frontier')
    if not s['best_record'] and s['logical_calls']<s['budget'] and not a.strict_time_limit:
        atomic_json(a.out/'primary_summary.json',s)
        s=recover(s,a.out/'recovery')
        s['time_protocol'].update(primary_seconds=a.seconds,primary_single_timeout=a.timeout)
        atomic_json(a.out/'summary.json',s)
    print(json.dumps(dict(calls=s['logical_calls'],best=score(s['best_record']) if s['best_record'] else None,
                         recovered='time_protocol' in s)))
