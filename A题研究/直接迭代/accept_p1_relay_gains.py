"""Independently replay faster warm-probe plans into a separate result folder.

The current 1500-plan release is never overwritten. Keep both old and new
plans when a time improvement increases COPY.
"""
import argparse
from pathlib import Path

from common_run import DATA, atomic_json, evaluate, read_json, score, write_csv
from persistent_budget import exact_signature


def deterministic_metrics(record):
    return {k: v for k, v in record['metrics'].items() if k != 'peak_memory_bytes'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent/'P1接力实验_20260926')
    args = parser.parse_args()
    root = args.root.resolve()
    audit = read_json(root/'run_v1/分析汇总.json')
    if not audit['analysis_final'] or audit['integrity_issues']:
        raise ValueError('only a completed, consistent batch can be accepted')
    out = root/'独立复评与新增方案'
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for candidate in audit['new_best_candidates']:
        case = candidate['case']
        old = read_json(candidate['record_path'])
        plan = read_json(candidate['plan_path'])
        saved = out/(case+'.verification.json')
        if saved.exists():
            record = read_json(saved)['replay_record']
        else:
            record = evaluate(DATA/(case+'.json'), plan, 1, out/'evaluations'/case,
                              timeout=60, config_path=DATA/'config.txt')
        if record['status'] != 'success':
            atomic_json(saved, dict(accepted=False, candidate=candidate, replay_record=record))
            raise ValueError(f'{case}: independent evaluation failed')
        if deterministic_metrics(old) != deterministic_metrics(record):
            raise ValueError(f'{case}: deterministic metrics mismatch')
        for key in ('graph_sha256', 'config_sha256', 'official_py_sha256'):
            if old['hashes'][key] != record['hashes'][key]:
                raise ValueError(f'{case}: input/evaluator mismatch')
        if exact_signature(read_json(record['plan_path'])) != exact_signature(plan):
            raise ValueError('plan encoding mismatch')
        if score(record) >= tuple(candidate['selected_score']):
            raise ValueError('candidate no longer improves the selected reference')
        plan_path = out/(case+'_p1_n5.json')
        atomic_json(plan_path, plan)
        atomic_json(saved, dict(accepted=True, candidate=candidate, replay_record=record,
                               deterministic_metrics_match=True, exported_plan=str(plan_path)))
        old_time, old_copy = candidate['selected_score']
        new_time, new_copy = score(record)
        row = dict(case=case, problem=1, cores=5, old_makespan=old_time, makespan=new_time,
                   time_reduction_percent=100*(1-new_time/old_time), old_added_copy_bytes=old_copy,
                   added_copy_bytes=new_copy, added_copy_delta=new_copy-old_copy,
                   copy_increase_percent=100*(new_copy/old_copy-1) if old_copy else None,
                   verified=True, plan_path=str(plan_path), verification_path=str(saved))
        rows.append(row)
        print(case, score(record), 'independent official replay matched', flush=True)
    write_csv(out/'新增方案.csv', rows)
    atomic_json(out/'summary.json', dict(complete=True, accepted=len(rows), rows=rows,
        scope='Additional time-first alternatives; original release kept; COPY tradeoffs explicit',
        independent_replays=len(rows), total_batch_official_calls=audit['costs']['official_recorded_calls_total']+len(rows)))


if __name__ == '__main__':
    main()
