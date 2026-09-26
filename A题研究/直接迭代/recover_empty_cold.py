"""Recover a cold run with no successful evaluation, retaining its paid calls.

This is an explicit extended-time fallback, not a claim that the original
240-second run succeeded. It first bounds P1 Task size, then retries original
timed-out plans; no historical selected solution is used. Every evaluation
stays within the original total call budget.
"""
import argparse
import copy
from pathlib import Path
import time

from common_run import DATA, GraphIR, atomic_json, evaluate, read_json, score


def recover(summary, out, seconds=480, timeout=180):
    if summary.get('best_record') is not None:
        raise ValueError('recovery is only for runs with no successful result')
    if any(c['record']['status'] == 'success' for c in summary['calls']):
        raise ValueError('inconsistent empty run')
    out = Path(out); out.mkdir(parents=True, exist_ok=False)
    s = copy.deepcopy(summary); original_count = len(s['calls'])
    if original_count != s['logical_calls'] or original_count >= s['budget']:
        raise ValueError('no unused original call budget')
    started = time.monotonic(); deadline = started+seconds
    if s['problem'] == 1:
        from bounded_task_fallback import plan as bounded_plan
        ir = GraphIR.from_path(DATA/(s['case']+'.json'))
        for cap in (512, 128):
            if len(s['calls']) >= s['budget'] or time.monotonic() >= deadline: break
            candidate = bounded_plan(ir, s['cores'], cap)
            record = evaluate(DATA/(s['case']+'.json'), candidate, s['problem'], out/'evaluations',
                timeout=min(60, max(.1, deadline-time.monotonic())), config_path=DATA/'config.txt')
            s['calls'].append(dict(name=f'bounded_task_feasibility_{cap}',
                metadata=dict(mechanism='bounded topological Task size; round-robin ownership', cap=cap),
                record=record, accepted=record['status'] == 'success'))
            if record['status'] == 'success':
                s['best_record'] = record
                break
    # If bounded Tasks also fail, retry only the original timed-out plans.
    original = sorted(summary['calls'], key=lambda c: ('whole_graph_single_active' not in c['name'],))
    seen = set()
    for call in original:
        if s['best_record'] is not None: break
        old = call['record']; h = old['hashes']['plan_sha256']
        if h in seen or old['status'] != 'timeout': continue
        seen.add(h)
        if len(s['calls']) >= s['budget'] or time.monotonic() >= deadline: break
        plan = read_json(old['plan_path'])
        record = evaluate(DATA/(s['case']+'.json'), plan, s['problem'], out/'evaluations',
            timeout=min(timeout, max(.1, deadline-time.monotonic())), config_path=DATA/'config.txt')
        for field in ('plan_sha256', 'graph_sha256', 'config_sha256', 'official_py_sha256'):
            if record['hashes'][field] != old['hashes'][field]:
                raise ValueError(('recovery input changed', field))
        s['calls'].append(dict(name='timeout_recovery_'+call['name'],
            metadata=dict(retries_attempt=old['attempt_id'], mechanism='longer evaluation timeout; no new search candidate'),
            record=record, accepted=record['status'] == 'success'))
        if record['status'] == 'success':
            s['best_record'] = record
            break
    s['logical_calls'] = len(s['calls'])
    extra = time.monotonic()-started
    s['elapsed_seconds'] += extra
    s['time_protocol'] = dict(primary_seconds=240, primary_single_timeout=60,
        recovery_seconds=seconds, recovery_single_timeout=timeout,
        trigger='no successful result after the primary cold solve',
        bounded_task_caps=[512, 128] if s['problem'] == 1 else [],
        original_calls_retained=original_count, recovery_calls=len(s['calls'])-original_count,
        recovery_elapsed_seconds=extra, total_call_budget=s['budget'])
    s['complete'] = True
    atomic_json(out/'summary.json', s)
    return s


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--summary', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True); p.add_argument('--seconds', type=float, default=480)
    p.add_argument('--timeout', type=float, default=180); a = p.parse_args()
    s = recover(read_json(a.summary), a.out, a.seconds, a.timeout)
    print(dict(case=s['case'], problem=s['problem'], cores=s['cores'],
        calls=s['logical_calls'], best=score(s['best_record']) if s['best_record'] else None,
        time_protocol=s['time_protocol']), flush=True)
