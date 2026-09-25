"""Compare official timelines of an old portfolio plan and a WCC winner."""
import argparse
import gzip
from common_run import *


def merged(intervals):
    out=[]
    for a,b in sorted(intervals):
        if b<a:raise ValueError('Negative timeline interval')
        if out and a<=out[-1][1]:out[-1]=(out[-1][0],max(b,out[-1][1]))
        else:out.append((a,b))
    return out


def intersection(a,b):
    a,b=merged(a),merged(b);i=j=total=0
    while i<len(a) and j<len(b):
        total+=max(0,min(a[i][1],b[j][1])-max(a[i][0],b[j][0]))
        if a[i][1]<=b[j][1]:i+=1
        else:j+=1
    return total


def inspect(record,ir):
    if record['status']!='success':raise ValueError('Successful records required')
    plan=read_json(record['plan_path']);validate_plan(ir,plan)
    sgcore={sg:c for c,s in enumerate(plan['core_schedules']) for sg in s}
    assignment={int(op):sgcore[sg] for op,sg in plan['node_to_subgraph'].items()}
    with gzip.open(record['result_path'],'rt') as f:raw=json.load(f)
    if raw['makespan']!=score(record)[0] or raw['problem']!=record['problem']:raise ValueError('Raw result mismatch')
    observed={};timing=[]
    for core in raw['per_core_timeline']:
        compute=[o for o in core['ops'] if o['op_id'] in ir.ops and o['op_id'] in assignment]
        for o in compute:
            if o['op_id'] in observed:raise ValueError('Duplicate original compute operation')
            observed[o['op_id']]=core['core_id']
        m=[(o['start'],o['end']) for o in compute if o['pipe']=='PIPE_M']
        v=[(o['start'],o['end']) for o in compute if o['pipe']=='PIPE_V']
        timing.append(dict(core=core['core_id'],end=max((o['end'] for o in core['ops']),default=0),
            m_busy=sum(b-a for a,b in merged(m)),v_busy=sum(b-a for a,b in merged(v)),mv_overlap=intersection(m,v)))
    if observed!=assignment:raise ValueError('Timeline compute assignment mismatch')
    return dict(record=record['record_path'],result=record['result_path'],plan=record['plan_path'],
        makespan=score(record)[0],data_movement_bytes=record['metrics']['data_movement_bytes'],
        cache_stats=record['metrics'].get('cache_stats'),memory_peak_by_core=record['metrics']['memory_peak_by_core'],
        core_timing=timing),assignment


def main(before,after_summary,out):
    s=read_json(after_summary);after=s['best_record'];k=key(after)
    old=next(r for r in read_json(before)['records'] if key(r)==k)
    ir=GraphIR.from_path(DATA/(k[0]+'.json'))
    a,aa=inspect(old,ir);b,ba=inspect(after,ir)
    winners=[dict(call=i,name=t['name'],phase=t['phase']) for i,t in enumerate(s['calls'],1)
             if t['record']['status']=='success' and score(t['record'])==score(after) and t['accepted']]
    result=dict(case=k[0],problem=k[1],cores=k[2],before=a,after=b,
        same_core_assignment=aa==ba,changed_compute_core_count=sum(aa[x]!=ba[x] for x in aa),
        component_count=len(ir.components),winning_calls=winners,
        reduction_pct=100*(1-b['makespan']/a['makespan']),
        scope='Observed paired timeline differences, not a causal attribution of each delay; original compute operations only for M/V overlap.')
    atomic_json(Path(out),result);print(json.dumps(result,ensure_ascii=False))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--before',required=True);p.add_argument('--after-summary',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();main(a.before,a.after_summary,a.out)
