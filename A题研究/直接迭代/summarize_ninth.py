"""Keep observed-routing comparisons separate from the cumulative portfolio."""
import argparse
import statistics
from common_run import *
from summarize_eighth import load_panel


def main(development, validation, out, old_regression=None):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    comparisons, wide, costs, decisions = [], [], [], []
    groups = [('development', development), ('validation', validation)]
    if old_regression is not None:
        groups.append(('prior_validation_regression', old_regression))
    for group, source in groups:
        rows, cost = load_panel(source, group)
        costs.append(cost)
        by = {(r['case'], r['problem'], r['method']): r for r in rows}
        for case, problem in sorted({(r['case'], r['problem']) for r in rows}):
            row = dict(group=group, case=case, problem=problem)
            for method in ('staged', 'routed', 'trace_routed'):
                r = by[case, problem, method]
                for field in ('makespan', 'status', 'logical_calls', 'new_calls', 'elapsed_seconds',
                              'stop_reason', 'failed_calls', 'timeout_calls', 'generation_errors'):
                    row[method+'_'+field] = r[field]
            trial = by[case, problem, 'trace_routed']
            if trial['status'] == 'success':
                s = read_json(trial['summary_path'])
                route = s.get('routing') or {}
                features = route.get('features') or {}
                probe = route.get('probe_selection') or {}
                row.update(route=s['effective_method'], reason=route.get('reason'),
                           headroom=features.get('fixed_assignment_headroom'),
                           probe_name=probe.get('name'), decision_after_calls=route.get('decision_after_calls'))
                decisions.append(dict(group=group, case=case, problem=problem, routing=route,
                    winning_calls=[dict(call=i, name=c['name'], phase=c['phase'])
                        for i,c in enumerate(s['calls'],1) if c['record']['status']=='success'
                        and c['accepted'] and score(c['record'])==score(s['best_record'])]))
            wide.append(row)
        for problem in (2,3):
            cases = sorted({r['case'] for r in rows if r['problem']==problem})
            for control in ('staged', 'routed'):
                pairs = [(by[c,problem,'trace_routed'],by[c,problem,control]) for c in cases]
                valid = [(a,b) for a,b in pairs if a['status']==b['status']=='success']
                gains = [100*(1-a['makespan']/b['makespan']) for a,b in valid]
                losses = [(100*(a['makespan']/b['makespan']-1),a['case'])
                          for a,b in valid if a['makespan']>b['makespan']]
                worst = max(losses,default=(0,None))
                comparisons.append(dict(group=group, problem=problem, method='trace_routed', control=control,
                    selected=len(pairs),valid_pairs=len(valid),excluded=len(pairs)-len(valid),
                    wins=sum(a['makespan']<b['makespan'] for a,b in valid),
                    ties=sum(a['makespan']==b['makespan'] for a,b in valid),losses=len(losses),
                    mean_paired_reduction_pct=statistics.mean(gains) if gains else None,
                    median_paired_reduction_pct=statistics.median(gains) if gains else None,
                    max_loss_pct=worst[0],max_loss_case=worst[1]))
    write_csv(out/'轨迹路线逐图比较.csv',wide)
    write_csv(out/'轨迹路线比较汇总.csv',comparisons)
    write_csv(out/'轨迹路线批次成本.csv',costs)
    atomic_json(out/'轨迹路线分析.json',dict(comparisons=comparisons,costs=costs,decisions=decisions,
        scope='Fixed current-run timeline rule, charged one-candidate probe, one total budget; '
              'development and preselected validation separate; successful pairs only with exclusions explicit.'))
    print(json.dumps(comparisons,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--development',required=True);p.add_argument('--validation',required=True);p.add_argument('--out',required=True)
    p.add_argument('--old-regression')
    a=p.parse_args();main(a.development,a.validation,a.out,a.old_regression)
