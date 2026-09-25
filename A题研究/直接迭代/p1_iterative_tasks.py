"""Refresh the teammate's Task neighbourhood after each accepted improvement.

Same official call/deadline budget throughout. The generator and official
evaluator are unchanged. A failed candidate does not trigger a retry.
"""
import gzip
import time
from common_run import *
from p1_joint_regions import exact_key
from p1_task_refine import generate
from p1_portfolio import refinement_caps


def search(initial_plan, initial_record, candidate_factory, evaluator, budget,
           deadline, on_progress=None):
    """Budgeted accept-and-regenerate loop, independently testable."""
    best_plan, best = initial_plan, initial_record
    seen = {exact_key(initial_plan)}
    trials, skipped, generations = [], [], 0
    while len(trials) < budget and time.monotonic() < deadline:
        parent_score = score(best)
        pool = candidate_factory(best_plan, best)
        generations += 1
        accepted = False
        for candidate in pool:
            if len(trials) >= budget or time.monotonic() >= deadline:
                break
            signature = exact_key(candidate['plan'])
            if signature in seen:
                skipped.append(dict(name=candidate['name'], reason='exact_duplicate',
                                    generation=generations))
                continue
            seen.add(signature)
            bound = candidate['metadata']['lower_bound']
            if bound > score(best)[0]:
                skipped.append(dict(name=candidate['name'], reason='necessary_bound',
                                    bound=bound, generation=generations))
                continue
            record = evaluator(candidate['plan'], deadline-time.monotonic())
            if record['status'] == 'success' and bound > score(record)[0]:
                raise AssertionError('unsafe candidate bound')
            accepted = record['status'] == 'success' and score(record) < score(best)
            trials.append(dict(name=candidate['name'], generation=generations,
                               parent_score=parent_score, record=record,
                               metadata=candidate['metadata'], accepted=accepted))
            if accepted:
                best_plan, best = candidate['plan'], record
            if on_progress:
                on_progress(best_plan, best, len(trials), generations)
            if accepted:
                break
        if not accepted:
            break
    return best_plan, best, trials, skipped, generations


def run(case, old, out, budget=7, seconds=180, cores=5, seed=17):
    if key(old) != (case, 1, cores) or old['status'] != 'success':
        raise ValueError('successful matching incumbent required')
    if budget < 1 or seconds <= 0:
        raise ValueError('positive call/time budgets required')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    deadline = start + seconds
    ir = GraphIR.from_path(DATA/(case+'.json'))
    generation_log = []

    def candidates(plan, record):
        with gzip.open(record['result_path'], 'rt') as f:
            raw = json.load(f)
        caps = refinement_caps(plan, cores)
        pool = generate(ir, plan, raw, seed, merge_caps=caps) if caps else []
        pool += generate(ir, plan, raw, seed)
        generation_log.append(dict(parent=record['plan_path'], score=score(record),
                                   candidates=[c['name'] for c in pool]))
        return pool

    def evaluate_candidate(plan, remaining):
        validate_plan(ir, plan)
        return evaluate(DATA/(case+'.json'), plan, 1, out/'evaluations',
                        timeout=min(60, max(.001, remaining)),
                        config_path=DATA/'config.txt')

    def progress(plan, record, calls, generations):
        atomic_json(out/'best.plan.json', plan)
        atomic_json(out/'progress.json', dict(case=case, calls=calls,
                    generations=generations, before=score(old)[0], after=score(record)[0]))

    plan, best, trials, skipped, generations = search(
        read_json(old['plan_path']), old, candidates, evaluate_candidate,
        budget, deadline, progress)
    atomic_json(out/'best.plan.json', plan)
    result = dict(case=case, problem=1, num_cores=cores, method='iterative_tasks',
                  before=score(old)[0], after=score(best)[0], best_record=best,
                  evaluations=trials, skipped=skipped, generations=generation_log,
                  logical_calls=len(trials), new_calls=sum(not t['record']['cache_hit'] for t in trials),
                  budget=budget, elapsed_seconds=time.monotonic()-start,
                  stop_reason='time_budget' if time.monotonic() >= deadline else
                              'call_budget' if len(trials) >= budget else 'neighbourhood_exhausted',
                  scope='extra-budget warm refinement; existing Task generator, accepted-parent refresh')
    atomic_json(out/'summary.json', result)
    return result
