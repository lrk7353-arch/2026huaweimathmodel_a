"""Summarize a fixed-call-cap panel by graph, keeping seed dependence explicit."""
import argparse
import statistics
from collections import Counter
from common_run import read_json, atomic_json, write_csv, Path, json


def analyze(directory):
    directory = Path(directory)
    summary = read_json(directory / 'summary.json')
    protocol = read_json(directory / 'plan.json')
    rows = summary['rows']
    methods = protocol.get('methods', ['component', 'legacy', 'adaptive'])
    reference = protocol.get('reference', 'adaptive')
    controls = [m for m in methods if m != reference]
    assert summary['complete'] and len(rows) == len(protocol['jobs'])
    assert len({(r['case'], r['method'], r['seed']) for r in rows}) == len(rows)
    medians, diversity = [], []
    for case in protocol['cases']:
        values = {}
        for method in methods:
            group = [r for r in rows if r['case'] == case and r['method'] == method]
            assert {r['seed'] for r in group} == set(protocol['seeds'])
            valid = [r for r in group if r['makespan'] is not None]
            values[method] = statistics.median(r['makespan'] for r in group) if len(valid)==len(group) else None
            # Serialization preserves insertion order because it can affect the evaluator.
            plans = {json.dumps(read_json(r['winner_plan']), separators=(',', ':')) for r in valid}
            diversity.append(dict(case=case, method=method, seeds=len(group),
                                  feasible_seeds=len(valid),distinct_winning_plans=len(plans),
                                  min_makespan=min((r['makespan'] for r in valid),default=None),
                                  max_makespan=max((r['makespan'] for r in valid),default=None)))
        medians.append(dict(case=case, **values, **{
            reference + '_vs_' + m + '_reduction_pct': 100 * (1 - values[reference] / values[m]) if values[reference] is not None and values[m] is not None else None
            for m in controls}))
    comparisons = {}
    for method in controls:
        paired=[r for r in medians if r[reference] is not None and r[method] is not None]
        comparisons[method] = dict(
            valid_pairs=len(paired),reference_only_feasible=sum(r[reference] is not None and r[method] is None for r in medians),
            control_only_feasible=sum(r[reference] is None and r[method] is not None for r in medians),
            neither_feasible=sum(r[reference] is None and r[method] is None for r in medians),
            wins=sum(r[reference] < r[method] for r in paired),
            ties=sum(r[reference] == r[method] for r in paired),
            losses=sum(r[reference] > r[method] for r in paired),
            mean_paired_time_reduction_pct=statistics.mean(r[reference + '_vs_' + method + '_reduction_pct'] for r in paired) if paired else None)
    result = dict(graphs=len(medians), slots=len(rows), reference=reference, comparisons=comparisons,
                  logical_calls=sum(r['logical_calls'] for r in rows),
                  fresh_calls=sum(r['new_calls'] for r in rows),
                  timeouts=sum(r['timeouts'] for r in rows),
                  infeasible_slots=sum(r['makespan'] is None for r in rows),
                  time_budget_stops=sum(r.get('stop_reason')=='time_budget' for r in rows),
                  batch_wall_seconds=summary['wall_seconds'],
                  diversity={m: dict(Counter(r['distinct_winning_plans'] for r in diversity if r['method'] == m))
                             for m in methods},
                  scope='selected graphs, repeated seeds per graph; graph-level median; shared cache; not cold runtime or full-100 evidence')
    write_csv(directory / '按图比较.csv', medians)
    write_csv(directory / '种子差异.csv', diversity)
    atomic_json(directory / 'analysis.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('directory')
    analyze(p.parse_args().directory)
