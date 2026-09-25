"""Read-only diagnostics of a saved P3 portfolio; no official evaluations."""
import argparse,gzip
from collections import Counter
from common_run import *
from advanced_solver.cache_refine import generate_cache_candidates,_core_map


def diagnose(record):
    case,problem,cores=key(record)
    ir=GraphIR.from_path(record['graph_path']);plan=read_json(record['plan_path'])
    with gzip.open(record['result_path'],'rt') as f:raw=json.load(f)
    _,diag=generate_cache_candidates(ir,plan,raw,num_cores=cores,max_candidates=1,seed=17)
    assignment=_core_map(plan);work=Counter()
    for op in ir.compute_ids:work[assignment[op],ir.ops[op]['pipe']]+=max(1,ir.ops[op]['cycles'])
    bound=max(work.values(),default=0);counts=diag['cache_access_counts'];targets=diag['ranked_targets']
    row=dict(case=case,cores=cores,ops=len(ir.compute_ids),components=len(ir.components),makespan=raw['makespan'],
             fixed_assignment_compute_bound=bound,headroom_pct=100*(1-bound/raw['makespan']),
             hits=counts.get('hit',0),concurrent_misses=counts.get('concurrent_cold_miss',0),
             post_eviction_misses=counts.get('post_eviction_miss',0),first_misses=counts.get('first_access_miss',0),
             hit_rate=raw['cache_stats']['hit_rate'],targets=diag['ranked_target_count'],
             top_target_score=max((t['priority_score'] for t in targets),default=0),
             top_target_relative=max((t['priority_score'] for t in targets),default=0)/raw['makespan'],
             repeated_tensors=diag['repeated_logical_tensors'])
    return row,diag


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--before',required=True);p.add_argument('--out',required=True);a=p.parse_args()
    out=Path(a.out);out.mkdir(parents=True,exist_ok=False);rows=[]
    records=[r for r in read_json(a.before)['records'] if r['problem']==3 and r['metrics']['num_cores']==5]
    for record in sorted(records,key=key):
        row,diag=diagnose(record);rows.append(row);atomic_json(out/(row['case']+'.json'),diag)
    write_csv(out/'results.csv',rows);atomic_json(out/'summary.json',dict(complete=True,completed=len(rows),expected=len(records),rows=rows,official_calls=0))
    for row in sorted(rows,key=lambda r:-r['top_target_relative'])[:18]:print(json.dumps(row),flush=True)
