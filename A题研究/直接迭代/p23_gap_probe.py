"""Four frozen graph-only P23 ownership/order probes; not a production solver.

No historical plans or scores participate in candidate generation. Every
official attempt is charged; no retry, and no more than two workers.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import time

from common_run import DATA, GraphIR, atomic_json, evaluate, score
from advanced_solver.operation_assign import generate_operation_candidates
from event_frontier import candidates as frontier_candidates


def run_one(args):
    case, problem, root = args
    out = Path(root) / case / f'p{problem}'
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    deadline = started + 240
    ir = GraphIR.from_path(DATA / f'{case}.json')
    generated, diagnostics = generate_operation_candidates(ir, 5, max_candidates=24, seed=17)
    by_name = {candidate['name']: candidate for candidate in generated}
    proposals = []
    generation_start = time.monotonic()
    stable = by_name['op_stable_id_w200']
    proposals.append(stable)
    for name in ['op_stable_id_w200', 'op_critical_path_w200', 'op_release_priority_w200']:
        source = by_name[name]
        proposal = next(frontier_candidates(ir, problem, 5, source['plan'], deadline))
        proposal['name'] = name + '_fixed_insertion'
        proposal['metadata']['source_family'] = source['metadata']
        proposals.append(proposal)
    generation_seconds = time.monotonic() - generation_start
    calls, best = [], None
    for proposal in proposals:
        if time.monotonic() >= deadline:
            break
        trial = dict(name=proposal['name'], metadata=proposal['metadata'])
        calls.append(trial)
        try:
            record = evaluate(ir.path, proposal['plan'], problem, out / 'evaluations',
                              timeout=min(60, max(.001, deadline-time.monotonic())),
                              config_path=DATA/'config.txt')
        except Exception as exc:
            record = dict(status='wrapper_exception', error=repr(exc), cache_hit=False)
        trial['record'] = record
        if record['status'] == 'success' and (best is None or score(record) < score(best)):
            best = record
        summary = dict(case=case, problem=problem, cores=5, budget=4,
                       seconds=240, timeout=60, calls=calls, logical_calls=len(calls),
                       best_record=best, elapsed_seconds=time.monotonic()-started,
                       generation_seconds=generation_seconds, diagnostics=diagnostics,
                       scope='frozen development probes; graph-only generation; no historical plans; not complete B24 comparison')
        atomic_json(out/'summary.json', summary)
    return dict(case=case, problem=problem, summary=str(out/'summary.json'), calls=len(calls),
                best=score(best) if best else None)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    jobs = [(f'case_{case:03d}', problem, str(args.out.resolve()))
            for case in [19,35,49,50,66] for problem in [2,3]]
    results=[]
    with ProcessPoolExecutor(max_workers=2) as pool:
        for result in pool.map(run_one, jobs):
            results.append(result)
            atomic_json(args.out/'results.json', results)
            print(json.dumps(result), flush=True)
