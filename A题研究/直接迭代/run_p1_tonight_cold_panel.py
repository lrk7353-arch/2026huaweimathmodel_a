"""Paired original-graph comparison, with bounded calls and no history input.

Each configuration runs both routes concurrently on this host. Start a new pair
only when the batch has room for both full per-arm budgets. No automatic retry.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import fcntl
import json
from pathlib import Path
import platform
import time
import traceback

from common_run import DATA, GraphIR, atomic_json, read_json, score, validate_plan, write_csv, evaluate
from accept_p1_relay_gains import deterministic_metrics
from persistent_budget import exact_signature


METHODS = ('integrated', 'joint_tonight')


def one(job):
    config, method, root, budget, seconds, timeout = job
    out = Path(root)/'slots'/config['id']/method
    try:
        from cold_portfolio import run
        summary = run(config['case'], 1, config['cores'], method, out, budget, seconds, timeout)
        calls = summary['calls']
        if len(calls) != summary['logical_calls'] or len(calls) > budget:
            raise ValueError('full-route call ledger mismatch')
        best = summary.get('best_record')
        if best and best['record_path'] not in {c['record'].get('record_path') for c in calls
                                               if c['record']['status']=='success'}:
            raise ValueError('best result was not paid for in this arm')
        return dict(config=config['id'], method=method, complete=True, status=summary['status'],
            makespan=score(best)[0] if best else None, added_copy=score(best)[1] if best else None,
            calls=len(calls), failed=sum(c['record']['status']!='success' for c in calls),
            elapsed_seconds=summary['elapsed_seconds'], stop_reason=summary['stop_reason'],
            generation_errors=sum(stage.get('phase')=='generation_error' for stage in summary['stages']),
            summary_path=str(out/'summary.json'))
    except Exception as error:
        atomic_json(out/'exception.json', dict(error=repr(error), traceback=traceback.format_exc()))
        records = list(out.glob('**/record.json'))
        return dict(config=config['id'], method=method, complete=False, status='exception',
                    calls=len(records), error=repr(error))


def replay_winners(out, panel):
    rows = []
    for config in panel['configurations']:
        left, right = (out/'slots'/config['id']/method/'summary.json' for method in METHODS)
        if not left.exists() or not right.exists(): continue
        a, b = read_json(left), read_json(right)
        old, new = a.get('best_record'), b.get('best_record')
        if not new or (old and score(new) >= score(old)): continue
        saved = out/'verification'/(config['id']+'.json')
        if saved.exists():
            result = read_json(saved)
            if result['source_record_path'] != new['record_path']:
                raise ValueError('independent replay source mismatch')
            rows.append(result); continue
        plan = read_json(new['plan_path'])
        ir = GraphIR.from_path(DATA/(config['case']+'.json')); validate_plan(ir, plan)
        record = evaluate(ir.path, plan, 1, out/'verification/evaluations'/config['id'],
                          timeout=60, config_path=DATA/'config.txt')
        matched = record['status']=='success' and deterministic_metrics(record)==deterministic_metrics(new)
        if matched:
            matched = exact_signature(read_json(record['plan_path'])) == exact_signature(plan)
            matched = matched and all(record['hashes'][key]==new['hashes'][key]
                for key in ('graph_sha256', 'config_sha256', 'official_py_sha256'))
        result = dict(config=config, verified=matched, source_record_path=new['record_path'],
                      source_score=list(score(new)), replay_record=record,
                      scope='Cold algorithm comparison winner; not a selected-library improvement claim')
        atomic_json(saved, result); rows.append(result)
    atomic_json(out/'verification/summary.json', dict(complete=True, independent_calls=len(rows), rows=rows))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--panel', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    out = args.out.resolve(); panel = read_json(args.panel)
    if args.verify:
        replay_winners(out, panel); return
    if panel['budget'] != 24 or panel['seconds'] != 240 or panel['workers'] != 2:
        raise ValueError('this panel uses the declared B24/240 protocol with 2 workers')
    if len({c['id'] for c in panel['configurations']}) != len(panel['configurations']):
        raise ValueError('duplicate configuration')
    out.mkdir(parents=True, exist_ok=False)
    with (out/'batch.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        atomic_json(out/'panel.json', dict(panel, platform=platform.platform(),
            started_local=time.strftime('%Y-%m-%d %H:%M:%S %Z'), cold_start=True))
        started = time.monotonic(); deadline = started+panel.get('batch_seconds', 2700)
        rows = []
        def save(complete=False):
            result = dict(complete=complete, completed=len(rows), expected=2*len(panel['configurations']),
                          actual_official_calls=sum(row['calls'] for row in rows), rows=rows,
                          seconds=time.monotonic()-started)
            atomic_json(out/'progress.json', result)
            if complete:
                atomic_json(out/'summary.json', result)
            write_csv(out/'results.csv', rows)
        save()
        with ProcessPoolExecutor(max_workers=2) as pool:
            for index, config in enumerate(panel['configurations']):
                if deadline-time.monotonic() < panel['seconds']+30:
                    break
                order = METHODS if index%2==0 else METHODS[::-1]
                jobs = [(config, method, str(out), panel['budget'], panel['seconds'], panel['timeout']) for method in order]
                futures = [pool.submit(one, job) for job in jobs]
                for future in as_completed(futures):
                    row = future.result(); rows.append(row); save()
                    print(json.dumps(row, ensure_ascii=False), flush=True)
        save(len(rows)==2*len(panel['configurations']) and all(row['complete'] for row in rows))


if __name__ == '__main__': main()
