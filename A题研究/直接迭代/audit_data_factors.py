"""Fresh 2x2 assignment/order interventions for a selected improved plan.

Both orders are encoded as one-op subgraphs; the observed-order baseline is
an explicit encoding control. A result is evidence for these selected plans,
not a population-level causal claim about cache hits or live-byte estimates.
"""
import argparse
import gzip
from common_run import *
from advanced_solver.trace_refine import _plan_assignment, _topology, _runs_plan, _trace, _tensor_views


def run(old, after, out):
    case,p,n=key(old)
    if key(after)!=(case,p,n) or p not in (2,3):raise ValueError('factor input mismatch')
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    ir=GraphIR.from_path(old['graph_path']);plans=[read_json(r['plan_path']) for r in (old,after)]
    owners=[_plan_assignment(ir,plan,n) for plan in plans]
    with gzip.open(old['result_path'],'rt') as f:raw=json.load(f)
    timings,_,_=_trace(ir,owners[0],raw,n,_tensor_views(ir),plans[0])
    old_order=_topology(ir,{o:(timings[o]['start'],timings[o]['end'],o) for o in ir.compute_ids})
    new_order=list(map(int,plans[1]['node_to_subgraph']))
    records={};rows=[];best=min((old,after),key=score);started=time.monotonic()
    atomic_json(out/'inputs.json',dict(before=old,after=after,encoding='all four cells single-op subgraphs'))
    for a in (0,1):
        for b,order in enumerate((old_order,new_order)):
            name=f'assignment{a}_order{b}'
            plan=_runs_plan(ir,order,owners[a],n,max_run_ops=1)
            if a==b==1 and json.dumps(plan,separators=(',',':'))!=json.dumps(plans[1],separators=(',',':')):
                raise ValueError('new plan cannot be exactly reconstructed')
            r=run_candidate(case,p,n,plan,out/'evaluations'/name,timeout=30)
            if r['status']!='success' or r['cache_hit']:raise RuntimeError('factor cell must be a fresh success')
            records[name]=r
            if score(r)<score(best):best=r
            m=r['metrics'];with_raw=json.load(gzip.open(r['result_path'],'rt'));cache=with_raw.get('cache_stats',{})
            rows.append(dict(case=case,problem=p,cores=n,assignment=a,order=b,makespan=m['makespan'],
                scheduled_copy_bytes=m['data_movement_bytes']['scheduled_copy_bytes'],
                spill_bytes=m['data_movement_bytes']['spill_added_copy_bytes'],
                cache_hits=cache.get('copy_in_hits',cache.get('hits')),cache_hit_bytes=cache.get('hit_bytes'),record=r['record_path']))
    interaction=rows[3]['makespan']-rows[2]['makespan']-rows[1]['makespan']+rows[0]['makespan']
    summary=dict(case=case,problem=p,num_cores=n,before=score(old)[0],after=score(best)[0],best_record=best,
        records=records,rows=rows,logical_calls=4,new_calls=4,interaction_cycles=interaction,
        elapsed_seconds=time.monotonic()-started,scope='selected-plan factorial intervention; encoding control explicit; selection is retrospective')
    atomic_json(out/'summary.json',summary);write_csv(out/'factors.csv',rows)
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--before-record',type=Path,required=True)
    p.add_argument('--after-record',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();s=run(read_json(a.before_record),read_json(a.after_record),a.out)
    print(json.dumps(dict(before=s['before'],after=s['after'],rows=s['rows']),ensure_ascii=False))
