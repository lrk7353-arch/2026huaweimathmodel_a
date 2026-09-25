"""Summarize two completed fixed-method P3 controller panels without winner mixing.

Only reads existing experiment records. Failed or missing method results remain
missing; they are never replaced by the incumbent or another method's winner.
"""
import argparse
import csv
import json
import math
import statistics
from collections import Counter
from pathlib import Path


CONTROLS = ('legacy', 'read_order', 'joint', 'interleave')


def read_json(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


def write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def finite_number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number: {value!r}')
    if value < 0 or (positive and value == 0):
        raise ValueError(f'{name} must be {"positive" if positive else "nonnegative"}.')
    return value


def optional_total(rows, field):
    values = [row.get(field) for row in rows]
    known = [value for value in values if value is not None]
    return dict(total=sum(known) if len(known) == len(values) else None,
                observed=sum(known), unknown_slots=len(values) - len(known))


def load_panel(path, group):
    path = Path(path).resolve()
    plan, panel = read_json(path / 'plan.json'), read_json(path / 'summary.json')
    if not panel.get('complete') or panel.get('completed') != panel.get('expected'):
        raise ValueError(f'{group} panel is not complete: {path}')
    methods, cases = plan['methods'], plan['cases']
    if len(set(methods)) != len(methods) or len(set(cases)) != len(cases):
        raise ValueError(f'{group} has duplicate planned methods or cases.')
    expected = {(case, method) for case in cases for method in methods}
    declared = [(row['case'], row['method']) for row in panel['rows']]
    if len(declared) != len(set(declared)) or set(declared) != expected:
        raise ValueError(f'{group} rows do not match the planned method/case grid.')
    if panel['expected'] != len(expected):
        raise ValueError(f'{group} expected count differs from the planned grid.')
    rows = []
    for case, method in sorted(expected):
        source = path / case / method / 'summary.json'
        result = read_json(source)
        declared_row = next(row for row in panel['rows'] if (row['case'], row['method']) == (case, method))
        for key in ('case', 'method', 'status', 'before', 'after', 'logical_calls', 'new_calls'):
            if result.get(key) != declared_row.get(key):
                raise ValueError(f'Panel/slot mismatch for {key}: {source}')
        if result.get('seed') != plan['seed'] or result.get('num_cores') != plan['cores']:
            raise ValueError(f'Slot seed/core count differs from the panel: {source}')
        before = finite_number(result['before'], f'{source}: before', positive=True)
        after = result.get('after')
        success = result['status'] == 'success'
        if success:
            finite_number(after, f'{source}: after', positive=True)
            if after > before:
                raise ValueError(f'Successful incumbent refinement regressed: {source}')
        elif after is not None:
            raise ValueError(f'Failed slot must not supply a substitute after value: {source}')
        calls = result.get('calls')
        statuses = Counter(call['record']['status'] for call in calls) if calls is not None else None
        logical, new = result.get('logical_calls'), result.get('new_calls')
        if logical is not None:
            finite_number(logical, f'{source}: logical_calls')
            if logical > plan['budget']:
                raise ValueError(f'Call budget exceeded: {source}')
        if calls is not None:
            if logical != len(calls) or new != sum(not call['record']['cache_hit'] for call in calls):
                raise ValueError(f'Call counts do not match trial records: {source}')
        if success and calls is None:
            raise ValueError(f'Successful slot is missing its trial records: {source}')
        row = dict(group=group, case=case, method=method, cores=plan['cores'], seed=plan['seed'],
                   before=before, after=after, status=result['status'],
                   logical_calls=logical, new_calls=new,
                   elapsed_seconds=finite_number(result['elapsed_seconds'], f'{source}: elapsed_seconds'),
                   stop_reason=result.get('stop_reason'),
                   eval_failed_calls=sum(n for status, n in statuses.items() if status != 'success') if statuses is not None else None,
                   eval_timeout_calls=statuses.get('timeout', 0) if statuses is not None else None,
                   eval_status_counts=dict(statuses) if statuses is not None else None,
                   fresh_evaluations=result.get('fresh_evaluations'),
                   error_type=result.get('error_type'), error=result.get('error'),
                   partial_progress=result.get('partial_progress'), source_summary=str(source))
        rows.append(row)
    for case in cases:
        if len({row['before'] for row in rows if row['case'] == case}) != 1:
            raise ValueError(f'{group}/{case} methods do not share a common incumbent makespan.')
    panel_info = dict(group=group, source=str(path), plan=plan, complete=True,
                      slots=len(rows), wall_seconds=finite_number(panel['wall_seconds'], f'{group}: wall_seconds'),
                      successful_slots=sum(row['status'] == 'success' for row in rows),
                      failed_slots=sum(row['status'] != 'success' for row in rows))
    return panel_info, rows


def compare(rows, group, control):
    selected = [row for row in rows if group == 'all' or row['group'] == group]
    by_slot = {(row['group'], row['case'], row['method']): row for row in selected}
    cases = sorted({(row['group'], row['case']) for row in selected})
    reductions, losses, excluded = [], [], []
    wins = ties = defeats = reference_only = control_only = neither = 0
    for source_group, case in cases:
        reference = by_slot.get((source_group, case, 'feedback'))
        other = by_slot.get((source_group, case, control))
        ref_ok = reference is not None and reference['status'] == 'success'
        other_ok = other is not None and other['status'] == 'success'
        if not (ref_ok and other_ok):
            reference_only += int(ref_ok and not other_ok)
            control_only += int(other_ok and not ref_ok)
            neither += int(not ref_ok and not other_ok)
            excluded.append(dict(group=source_group, case=case,
                                 feedback_status=reference['status'] if reference else 'not_planned',
                                 control_status=other['status'] if other else 'not_planned'))
            continue
        a, b = reference['after'], other['after']
        reductions.append(100 * (1 - a / b))
        wins += int(a < b)
        ties += int(a == b)
        defeats += int(a > b)
        if a > b:
            losses.append(dict(group=source_group, case=case, feedback=a, control=b,
                               loss_pct=100 * (a / b - 1), loss_cycles=a - b))
    worst = max(losses, key=lambda item: item['loss_pct'], default=None)
    summary = dict(group=group, method='feedback', control=control,
                   selected_graph_slots=len(cases), valid_pairs=len(reductions),
                   wins=wins, ties=ties, losses=defeats,
                   mean_paired_reduction_pct=statistics.mean(reductions) if reductions else None,
                   median_paired_reduction_pct=statistics.median(reductions) if reductions else None,
                   max_loss_pct=worst['loss_pct'] if worst else (0.0 if reductions else None),
                   max_loss_case=worst['case'] if worst else None,
                   max_loss_group=worst['group'] if worst else None,
                   max_loss_cycles=worst['loss_cycles'] if worst else (0 if reductions else None),
                   feedback_only_success=reference_only, control_only_success=control_only,
                   neither_success=neither, excluded_pairs=len(excluded))
    return summary, excluded


def cost_row(rows, group, method, wall_seconds=None):
    selected = [row for row in rows if row['group'] == group and (method == 'all' or row['method'] == method)]
    row = dict(group=group, method=method, slots=len(selected),
               successful_slots=sum(item['status'] == 'success' for item in selected),
               failed_slots=sum(item['status'] != 'success' for item in selected),
               wall_seconds=wall_seconds,
               sum_slot_elapsed_seconds=sum(item['elapsed_seconds'] for item in selected))
    for field in ('logical_calls', 'new_calls', 'eval_failed_calls', 'eval_timeout_calls'):
        values = optional_total(selected, field)
        row['total_' + field] = values['total']
        row['observed_' + field] = values['observed']
        row[field + '_unknown_slots'] = values['unknown_slots']
    return row


def main(regression, validation, out):
    out = Path(out).resolve()
    if not out.is_dir():
        raise ValueError('--out must be an existing result directory.')
    panels, rows = [], []
    for group, source in (('regression', regression), ('validation', validation)):
        info, source_rows = load_panel(source, group)
        panels.append(info)
        rows.extend(source_rows)
    if panels[0]['source'] == panels[1]['source']:
        raise ValueError('Regression and validation must be different panels.')
    wide = []
    for panel in panels:
        for case in sorted(panel['plan']['cases']):
            slots = [row for row in rows if row['group'] == panel['group'] and row['case'] == case]
            row = dict(group=panel['group'], case=case, before=slots[0]['before'],
                       cores=panel['plan']['cores'], seed=panel['plan']['seed'])
            for slot in slots:
                for field in ('after', 'status', 'logical_calls', 'new_calls', 'elapsed_seconds',
                              'stop_reason', 'eval_failed_calls', 'eval_timeout_calls', 'error_type'):
                    row[slot['method'] + '_' + field] = slot[field]
            wide.append(row)
    comparisons, exclusions = [], []
    for group in ('regression', 'validation', 'all'):
        for control in CONTROLS:
            summary, excluded = compare(rows, group, control)
            comparisons.append(summary)
            exclusions.append(dict(group=group, control=control, cases=excluded))
    costs = []
    for panel in panels:
        costs.append(cost_row(rows, panel['group'], 'all', panel['wall_seconds']))
        costs.extend(cost_row(rows, panel['group'], method) for method in panel['plan']['methods'])
    all_totals = {field: optional_total(rows, field) for field in
                  ('logical_calls', 'new_calls', 'eval_failed_calls', 'eval_timeout_calls')}
    overlap = sorted(set(panels[0]['plan']['cases']) & set(panels[1]['plan']['cases']))
    analysis = dict(schema_version=1, panels=panels, per_graph_method=rows,
                    comparisons=comparisons, excluded_comparison_pairs=exclusions, costs=costs,
                    totals=dict(slots=len(rows), successful_slots=sum(row['status'] == 'success' for row in rows),
                                failed_slots=sum(row['status'] != 'success' for row in rows),
                                sum_panel_wall_seconds=sum(panel['wall_seconds'] for panel in panels),
                                sum_slot_elapsed_seconds=sum(row['elapsed_seconds'] for row in rows), **all_totals),
                    overlapping_cases_between_panels=overlap,
                    definitions=dict(
                        paired_reduction_pct='100 * (control_after - feedback_after) / control_after; positive is better.',
                        win_tie_loss='Official makespan only; COPY tie-break improvements are not counted as time wins.',
                        max_loss_cycles='Cycle increase on the graph with greatest relative loss, not maximum absolute cycle increase.',
                        costs='Panel wall is measured elapsed wall time; method rows report sum of slot wall times, not parallel makespan.',
                        incomplete_costs='Null totals mean at least one failed slot lacks trial counts; observed totals are lower bounds.',
                        pooled='All means pool fixed panel graph slots, including any overlap; no best-run, best-seed or best-method selection.',
                        failures='Unsuccessful or unplanned method results are excluded from numerical paired averages and reported separately.',
                        scope='Both panels start from supplied warm incumbents. Fresh candidate caches do not make the whole solve cold.'))
    write_csv(out / '第七轮方法比较.csv', wide)
    write_csv(out / '比较汇总.csv', comparisons)
    write_csv(out / '运行成本.csv', costs)
    (out / 'analysis.json').write_text(json.dumps(analysis, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps(dict(out=str(out), slots=len(rows), comparisons=comparisons), ensure_ascii=False))
    return analysis


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--regression', required=True)
    parser.add_argument('--validation', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        main(args.regression, args.validation, args.out)
    except ValueError as exc:
        parser.error(str(exc))
