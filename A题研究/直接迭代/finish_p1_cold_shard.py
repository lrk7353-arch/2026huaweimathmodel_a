"""Finish a finite P1 shard and account for explicit empty-run recovery."""
import argparse
import concurrent.futures
import hashlib
from pathlib import Path
import time

from common_run import atomic_json, read_json, score
from recover_empty_cold import recover


def work(task):
    row, recovery_root = task
    source = read_json(row['summary'])
    path = Path(recovery_root)/'slots'/row['case']/f'p1_n{row["cores"]}'
    if (path/'summary.json').exists():
        s = read_json(path/'summary.json')
        original_ids = [c['record']['attempt_id'] for c in source['calls']]
        restored_ids = [c['record']['attempt_id'] for c in s['calls'][:len(original_ids)]]
        if original_ids != restored_ids: raise ValueError('recovered prefix mismatch')
    else:
        if path.exists(): raise ValueError(('another recovery is still running', str(path)))
        s = recover(source, path)
    return dict(case=s['case'], problem=s['problem'], cores=s['cores'], variant=s['variant'],
        summary=str((path/'summary.json').resolve()), best=score(s['best_record'])[0] if s['best_record'] else None,
        calls=s['logical_calls'], elapsed_seconds=s['elapsed_seconds'])


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--primary', type=Path, required=True)
    p.add_argument('--recovery', type=Path, required=True); p.add_argument('--out', type=Path, required=True)
    p.add_argument('--workers', type=int, default=2); p.add_argument('--wait', action='store_true'); a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    until = time.monotonic()+7200
    while not (a.primary/'completion.json').exists():
        if not a.wait or time.monotonic() >= until: raise ValueError('primary shard has not finished')
        time.sleep(5)
    complete = read_json(a.primary/'completion.json')
    if complete['completed'] != complete['total'] or complete['errors']:
        raise ValueError(('primary runner failed', complete))
    rows = read_json(a.primary/'results.json'); missing = [r for r in rows if r['best'] is None]
    atomic_json(a.out/'manifest.json', dict(primary=read_json(a.primary/'manifest.json'),
        primary_completion=complete, recovery_rule='only empty runs; retain initial paid calls; bounded topological Tasks 512/128 with60s each, then original timed-out plans with180s each; stop on first success; within B24 and additional480s',
        source_sha256={n:hashlib.sha256(Path(__file__).with_name(n).read_bytes()).hexdigest()
                       for n in ('recover_empty_cold.py', 'bounded_task_fallback.py', 'finish_p1_cold_shard.py')}))
    result = [r for r in rows if r['best'] is not None]
    with concurrent.futures.ProcessPoolExecutor(max_workers=a.workers) as pool:
        for row in pool.map(work, [(r, str(a.recovery)) for r in missing]):
            result.append(row); atomic_json(a.out/'results.json', result); print(row, flush=True)
    atomic_json(a.out/'results.json', result)
    state = dict(complete=all(r['best'] is not None for r in result), configurations=len(result),
        recovered_candidates=len(missing), missing_best=sum(r['best'] is None for r in result))
    atomic_json(a.out/'completion.json', state); print(state, flush=True)
