"""Read fixed P2/P3 panels; compare routed policy without best-method mixing."""
import argparse
import statistics
from common_run import *


def load_panel(path, group):
    path=Path(path);plan=read_json(path/'plan.json');panel=read_json(path/'summary.json')
    expected={(c,p,m) for c in plan['cases'] for p in plan['problems'] for m in plan['methods']}
    if not panel['complete'] or panel['completed']!=panel['expected']:raise ValueError('Incomplete panel')
    if len(panel['rows'])!=len(expected) or {(r['case'],r['problem'],r['method']) for r in panel['rows']}!=expected:
        raise ValueError('Incorrect method/case grid')
    rows=[]
    for row in panel['rows']:
        r=dict(row,group=group)
        if r['status']=='success':
            s=read_json(r['summary_path']);record=s['best_record']
            if key(record)!=(r['case'],r['problem'],r['cores']) or score(record)[0]!=r['makespan']:raise ValueError('Result identity mismatch')
            if len(s['calls'])!=r['logical_calls'] or r['logical_calls']>plan['budget']:raise ValueError('Incorrect call accounting')
            if sum(not c['record']['cache_hit'] for c in s['calls'])!=r['new_calls']:raise ValueError('Incorrect new-call accounting')
            r['routing_ratio']=(s.get('routing') or {}).get('component_to_ideal_ratio')
        rows.append(r)
    return rows,dict(group=group,source=str(path),configs=len(rows),failed=panel['failed'],
                     logical_calls=sum(r['logical_calls'] for r in rows) if all(r['logical_calls'] is not None for r in rows) else None,
                     new_calls=sum(r['new_calls'] for r in rows) if all(r['new_calls'] is not None for r in rows) else None,
                     wall_seconds=panel['wall_seconds'],fresh_evaluations=plan['fresh_evaluations'])


def analyze(regression, validation, out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    rows=[];costs=[]
    for group,path in [('regression',regression),('validation',validation)]:
        r,c=load_panel(path,group);rows.extend(r);costs.append(c)
    wide=[];comparisons=[];route_checks=[]
    for group in ('regression','validation'):
        ss=[r for r in rows if r['group']==group]
        by={(r['case'],r['problem'],r['method']):r for r in ss}
        for c,p in sorted({(r['case'],r['problem']) for r in ss}):
            row=dict(group=group,case=c,problem=p)
            for method in ('component_wcc','staged','routed'):
                r=by[c,p,method]
                for field in ('makespan','status','logical_calls','new_calls','elapsed_seconds','stop_reason','failed_calls','timeout_calls'):
                    row[method+'_'+field]=r[field]
            routed=by[c,p,'routed'];branch=routed['effective_method']
            row['chosen_route']=branch;row['routing_ratio']=routed.get('routing_ratio');wide.append(row)
            branch_row=by.get((c,p,branch))
            route_checks.append(dict(group=group,case=c,problem=p,route=branch,
                same_score=bool(branch_row and routed['status']==branch_row['status']=='success' and routed['makespan']==branch_row['makespan']),
                routed_stop=routed['stop_reason'],branch_stop=branch_row['stop_reason'] if branch_row else None))
        for p in (2,3):
            cases=sorted({r['case'] for r in ss if r['problem']==p})
            for control in ('component_wcc','staged'):
                pairs=[(by[c,p,'routed'],by[c,p,control]) for c in cases]
                valid=[(a,b) for a,b in pairs if a['status']==b['status']=='success']
                deltas=[100*(1-a['makespan']/b['makespan']) for a,b in valid]
                losses=[(100*(a['makespan']/b['makespan']-1),a['case']) for a,b in valid if a['makespan']>b['makespan']]
                worst=max(losses,default=(0,None))
                comparisons.append(dict(group=group,problem=p,method='routed',control=control,
                    selected=len(pairs),valid_pairs=len(valid),excluded=len(pairs)-len(valid),
                    wins=sum(a['makespan']<b['makespan'] for a,b in valid),ties=sum(a['makespan']==b['makespan'] for a,b in valid),
                    losses=len(losses),mean_paired_reduction_pct=statistics.mean(deltas) if deltas else None,
                    median_paired_reduction_pct=statistics.median(deltas) if deltas else None,max_loss_pct=worst[0],max_loss_case=worst[1]))
    write_csv(out/'结构路线逐图比较.csv',wide);write_csv(out/'结构路线比较汇总.csv',comparisons)
    write_csv(out/'结构路线批次成本.csv',costs)
    summary=dict(comparisons=comparisons,route_checks=route_checks,costs=costs,rows=rows,
                 scope='Fixed graph-only route; each actual method has one shared budget; mean paired reductions by problem and group; shared-cache times are not cold runtimes.')
    atomic_json(out/'结构路线分析.json',summary)
    print(json.dumps(dict(comparisons=comparisons,route_mismatches=[r for r in route_checks if not r['same_score']]),ensure_ascii=False))
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--regression',required=True);p.add_argument('--validation',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();analyze(a.regression,a.validation,a.out)
