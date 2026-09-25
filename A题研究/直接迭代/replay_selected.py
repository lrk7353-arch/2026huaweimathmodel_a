"""Fresh independent official evaluations of selected current best plans."""
import argparse,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from common_run import *


def one(record,out):
    case,p,n=key(record);name=f'{case}_p{p}_n{n}';start=time.monotonic()
    r=run_candidate(case,p,n,read_json(record['plan_path']),Path(out)/name,timeout=60)
    row=dict(case=case,problem=p,cores=n,status=r['status'],new_call=not r['cache_hit'],
             expected=score(record),actual=score(r) if r['status']=='success' else None,
             matches=r['status']=='success' and score(r)==score(record),elapsed_seconds=time.monotonic()-start,
             source_record=record['record_path'],record=r['record_path'])
    return row


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True)
    p.add_argument('--targets',required=True,help='case:problem:cores comma-separated, e.g.94:3:5,52:2:5');a=p.parse_args()
    out=Path(a.out);out.mkdir(parents=True,exist_ok=False);b=known();start=time.monotonic();rows=[]
    targets=[tuple(map(int,t.split(':'))) for t in a.targets.split(',')]
    records=[b[f'case_{c:03d}',p,n] for c,p,n in targets]
    atomic_json(out/'inputs.json',dict(records=records))
    with ThreadPoolExecutor(max_workers=2) as pool:
        fs=[pool.submit(one,r,out) for r in records]
        for f in as_completed(fs):rows.append(f.result())
    atomic_json(out/'summary.json',dict(complete=True,completed=len(rows),expected=len(records),rows=rows,wall_seconds=time.monotonic()-start))
    print(json.dumps(rows,ensure_ascii=False,indent=2))
    if not all(r['matches'] and r['new_call'] for r in rows):raise RuntimeError('fresh replay mismatch')
