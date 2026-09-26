"""Registered warm developmental P1 probe; never a from-scratch score."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import time

from common_run import DATA, GraphIR, atomic_json, evaluate
from p1_recipe_bootstrap import generation_limit
from persistent_p1_moves import iter_candidates
from wait_observation import observe


def run(case, out, merge_only=False):
    source = Path(__file__).parent/'运行结果/硬件启发从头P1B24_v1/slots'/case/'p1_n5/mature/summary.json'
    parent = json.loads(source.read_text())['best_record']
    plan = json.loads(Path(parent['plan_path']).read_text())
    with gzip.open(parent['result_path'], 'rt') as f: raw = json.load(f)
    ir = GraphIR.from_path(DATA/(case+'.json'))
    out = Path(out); out.mkdir(parents=True, exist_ok=False)
    manifest = dict(case=case, problem=1, cores=5, budget=2 if merge_only else 4, per_evaluation_seconds=60,
        generation_seconds=12, observation_seconds=12, parent_summary=str(source),
        parent_record=parent, round_index=0, merge_only=merge_only,
        source_sha256=hashlib.sha256(Path(__file__).with_name('persistent_p1_moves.py').read_bytes()).hexdigest(),
        scope='Warm developer-only legality/mechanism validation; parent/trace are historical inputs, not a cold result.')
    atomic_json(out/'manifest.json', manifest)
    started = time.monotonic(); errors = []; observation = None
    t = time.monotonic()
    try:
        with generation_limit(12): observation = observe(ir, plan, raw, 1)
    except Exception as exc: errors.append(dict(stage='observation', error=repr(exc)))
    observation_seconds = time.monotonic()-t
    candidates = iter_candidates(ir, plan, raw, 5, observation=observation, seconds=12)
    calls = []; skipped = []
    for _ in range(4):
        t = time.monotonic()
        try:
            with generation_limit(12): candidate = next(candidates)
        except StopIteration as done:
            if done.value:
                errors.extend(dict(stage='candidate_construction', **e) for e in done.value.get('generation_failures', []))
            break
        except Exception as exc:
            errors.append(dict(stage='generation', error=repr(exc), seconds=time.monotonic()-t)); break
        generation_seconds = time.monotonic()-t
        if merge_only and not candidate['metadata']['merged_tasks']:
            skipped.append(dict(name=candidate['name'], reason='registered merge-only control', metadata=candidate['metadata']))
            continue
        record = evaluate(ir.path, candidate['plan'], 1, out/'evaluations', timeout=60, config_path=DATA/'config.txt')
        calls.append(dict(name=candidate['name'], metadata=candidate['metadata'], record=record,
                          generation_seconds=generation_seconds))
        atomic_json(out/'summary.json', dict(manifest=manifest, calls=calls, errors=errors, skipped=skipped,
            logical_calls=len(calls), observation_seconds=observation_seconds,
            elapsed_seconds=time.monotonic()-started, complete=False))
        print(dict(case=case, name=candidate['name'], status=record['status'],
            metrics=record.get('metrics'), metadata=candidate['metadata']), flush=True)
    candidates.close()
    atomic_json(out/'summary.json', dict(manifest=manifest, calls=calls, errors=errors, skipped=skipped,
        logical_calls=len(calls), observation_seconds=observation_seconds,
        elapsed_seconds=time.monotonic()-started, complete=True))


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--case', required=True); p.add_argument('--out', required=True)
    p.add_argument('--merge-only', action='store_true')
    args = p.parse_args(); run(args.case, args.out, args.merge_only)
