"""Report paired original-graph runs, preserving failures and actual budgets."""
import argparse
import statistics
from common_run import *


def analyze(root):
    root=Path(root);panel=read_json(root/'summary.json');plan=read_json(root/'plan.json')
    if not panel['complete'] or panel['completed']!=panel['expected']:raise ValueError('panel not complete')
    rows=panel['rows'];by={(r['case'],r['problem'],r['cores'],r['method']):r for r in rows}
    pairs=[];curves=[];groups=[]
    for p in (1,2,3):
        comparisons=plan.get('comparisons',[('integrated','baseline'),('integrated','local_legacy'),('local_legacy','baseline'),
            ('budget_greedy','baseline'),('budget_beam','baseline'),('budget_greedy','local_legacy'),
            ('budget_beam','local_legacy'),('budget_beam','budget_greedy'),('wide_legacy','baseline'),
            ('wide_legacy','local_legacy'),('budget_greedy','wide_legacy'),('budget_beam','wide_legacy')])
        for method,control in comparisons:
            matched=[(r,by[c,p,n,control]) for (c,problem,n,m),r in by.items() if problem==p and m==method and (c,p,n,control) in by]
            if not matched:continue
            successful=[]
            for r,b in matched:
                ok=r['status']=='success' and b['status']=='success'
                pair=dict(case=r['case'],problem=p,cores=r['cores'],method=method,control=control,
                    method_status=r['status'],control_status=b['status'],method_time=r.get('makespan'),control_time=b.get('makespan'),
                    reduction_pct=100*(1-r['makespan']/b['makespan']) if ok else None)
                pairs.append(pair)
                if ok:successful.append(pair)
            gains=[r['reduction_pct'] for r in successful]
            groups.append(dict(problem=p,method=method,control=control,pairs=len(matched),successful_pairs=len(successful),
                method_failures=sum(r['status']!='success' for r,b in matched),control_failures=sum(b['status']!='success' for r,b in matched),
                wins=sum(r['method_time']<r['control_time'] for r in successful),ties=sum(r['method_time']==r['control_time'] for r in successful),
                losses=sum(r['method_time']>r['control_time'] for r in successful),mean_paired_reduction_pct=statistics.mean(gains) if gains else None,
                median_paired_reduction_pct=statistics.median(gains) if gains else None))
    for r in rows:
        if r['status']=='exception':continue
        s=read_json(r['summary_path']);calls=s.get('calls',s.get('evaluations',[]));best=None
        if len(calls)>plan['budget'] or len(calls)!=s['logical_calls']:raise ValueError('call accounting violated')
        if s['new_calls']!=len(calls):raise ValueError('panel expected all-fresh official attempts')
        for i,c in enumerate(calls,1):
            rec=c['record']
            if rec['status']=='success':
                if key(rec)!=(r['case'],r['problem'],r['cores']):raise ValueError('record identity mismatch')
                best=min(best,score(rec)[0]) if best is not None else score(rec)[0]
            curves.append(dict(case=r['case'],problem=r['problem'],cores=r['cores'],method=r['method'],call=i,
                               phase=c.get('phase'),status=rec['status'],best_makespan=best))
        if best!=r['makespan']:raise ValueError('best not obtained from charged calls')
    write_csv(root/'paired.csv',pairs);write_csv(root/'comparisons.csv',groups);write_csv(root/'curves.csv',curves)
    # Same upper caps do not imply equal actual calls when a pool exhausts or a
    # timeout interrupts search. Also compare both curves at their common count.
    histories={}
    for c in curves:histories.setdefault((c['case'],c['problem'],c['cores'],c['method']),[]).append(c)
    matched_calls=[]
    for pair in pairs:
        k=(pair['case'],pair['problem'],pair['cores'])
        a=histories.get(k+(pair['method'],),[]);b=histories.get(k+(pair['control'],),[])
        count=min(len(a),len(b));x=a[count-1]['best_makespan'] if count else None;y=b[count-1]['best_makespan'] if count else None
        matched_calls.append(dict(case=k[0],problem=k[1],cores=k[2],method=pair['method'],control=pair['control'],
            common_calls=count,method_time=x,control_time=y,reduction_pct=100*(1-x/y) if x is not None and y is not None else None))
    write_csv(root/'matched_calls.csv',matched_calls)
    common_groups=[]
    for g in groups:
        rr=[r for r in matched_calls if all(r[k]==g[k] for k in ('problem','method','control'))]
        valid=[r for r in rr if r['reduction_pct'] is not None]
        common_groups.append(dict(problem=g['problem'],method=g['method'],control=g['control'],pairs=len(rr),
            successful_pairs=len(valid),wins=sum(r['method_time']<r['control_time'] for r in valid),
            ties=sum(r['method_time']==r['control_time'] for r in valid),losses=sum(r['method_time']>r['control_time'] for r in valid),
            mean_paired_reduction_pct=statistics.mean(r['reduction_pct'] for r in valid) if valid else None))
    write_csv(root/'matched_call_comparisons.csv',common_groups)
    costs=[]
    for p in (1,2,3):
        for method in sorted({r['method'] for r in rows}):
            rr=[r for r in rows if r['problem']==p and r['method']==method]
            if not rr:continue
            costs.append(dict(problem=p,method=method,runs=len(rr),successes=sum(r['status']=='success' for r in rr),
                calls=sum(r.get('logical_calls',0) for r in rr),fresh=sum(r.get('new_calls',0) for r in rr),
                failed_calls=sum(r.get('failed_calls',0) for r in rr),generation_errors=sum(r.get('generation_errors',0) for r in rr),
                total_run_seconds=sum(r.get('elapsed_seconds',0) for r in rr),
                median_run_seconds=statistics.median(r.get('elapsed_seconds',0) for r in rr)))
    write_csv(root/'costs.csv',costs)
    result=dict(comparisons=groups,matched_call_comparisons=common_groups,costs=costs,wall_seconds=panel['wall_seconds'],budget=plan['budget'],seconds=plan['seconds'],
        scope='fresh from-original-graph evaluations; same upper caps, actual calls reported; no historical incumbents; mean excludes failed pairs explicitly counted')
    atomic_json(root/'analysis.json',result);return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--panel',type=Path,required=True);a=p.parse_args()
    print(json.dumps(analyze(a.panel),ensure_ascii=False,indent=2))
