"""Bounded warm comparison of old joint refinement and new regional moves.

Historical starts are replayed once, then shared by both arms. This diagnostic
is not a cold solver comparison. The original official evaluator is unchanged.
Completed jobs resume without re-evaluation; an interrupted paid call is
recovered from its own attempt directory or charged as interrupted.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import fcntl
import gzip
import json
import math
from pathlib import Path
import platform
import time

from common_run import DATA, GraphIR, atomic_json, evaluate, read_json, score, validate_plan, write_csv
from persistent_budget import exact_signature
from persistent_search import generation_limit


FAMILIES = ('legacy_joint', 'region_joint')


def proposals(ir, plan, raw, family, round_index):
    if family == 'legacy_joint':
        from p1_joint_regions import generate
        candidates, diagnostics = generate(ir, plan, raw, 5, limit=64, round_index=round_index)
        yield from candidates
        return diagnostics
    if family == 'region_joint':
        from persistent_p1_moves import iter_candidates
        return (yield from iter_candidates(ir, plan, raw, 5, round_index=round_index, seconds=24))
    raise ValueError('unknown family')


def assert_replay(seed, record):
    if record['status'] != 'success':
        raise ValueError('seed official replay failed: ' + record['status'])
    actual = score(record)
    expected = (seed['expected_makespan'], seed['expected_added_copy_bytes'])
    if actual != expected:
        raise ValueError(f'seed mismatch: expected {expected}, received {actual}')
    if record['metrics']['num_cores'] != 5:
        raise ValueError('seed core count mismatch')


def assert_trace(ir, plan, raw, record):
    """Reject stale same-shaped Task traces, including wrong op/core ownership."""
    if raw['makespan'] != score(record)[0] or raw['num_cores'] != 5:
        raise ValueError('raw trace does not match current parent metrics')
    mapping = {int(op): task for op, task in plan['node_to_subgraph'].items()}
    found = set()
    cores = {core['core_id']: core for core in raw['per_core_timeline']}
    if set(cores) != set(range(5)):
        raise ValueError('trace core coverage mismatch')
    for core, tasks in enumerate(plan['core_schedules']):
        entries = cores[core]
        if [t['task_id'] for t in entries['tasks']] != tasks:
            raise ValueError('trace Task sequence mismatch')
        for op in entries['ops']:
            oid = op['op_id']
            if oid in mapping:
                if oid in found or op['task_id'] != mapping[oid] or op['task_id'] not in tasks:
                    raise ValueError('trace compute ownership mismatch')
                found.add(oid)
    if found != set(ir.compute_ids):
        raise ValueError('trace compute coverage mismatch')


def _replay(job):
    seed, out, timeout, batch_deadline = job
    out = Path(out)
    path = out/'seed.json'
    if path.exists():
        saved = read_json(path)
        if saved.get('complete'):
            return saved
    started = time.monotonic()
    ir = GraphIR.from_path(DATA/(seed['case']+'.json'))
    plan = read_json(seed['plan_path'])
    validate_plan(ir, plan)
    out.mkdir(parents=True, exist_ok=True)
    # A prior completed attempt may have survived interruption before seed.json.
    prior = sorted((out/'evaluations'/'attempts').glob('*/record.json'))
    record = read_json(prior[-1]) if prior else None
    if record is None:
        remaining = batch_deadline-time.time()
        attempts = list((out/'evaluations'/'attempts').glob('*'))
        if attempts:
            record = dict(status='interrupted', metrics={}, elapsed_seconds=timeout)
        elif remaining <= 0:
            record = dict(status='batch_deadline', metrics={})
        else:
            record = evaluate(ir.path, plan, 1, out/'evaluations',
                              timeout=min(timeout, remaining), config_path=DATA/'config.txt')
    error = None
    try:
        assert_replay(seed, record)
        with gzip.open(record['result_path'], 'rt') as handle:
            assert_trace(ir, plan, json.load(handle), record)
    except Exception as exc:
        error = repr(exc)
    result = dict(seed=seed, record=record, complete=True, verified=error is None,
                  error=error, replay_wall_seconds=time.monotonic()-started)
    atomic_json(path, result)
    print(json.dumps(dict(event='seed', id=seed['id'], verified=result['verified'],
                          score=score(record) if result['verified'] else None,
                          error=error), ensure_ascii=False), flush=True)
    return result


def _recover_pending(state, out):
    pending = state.pop('pending', None)
    if pending is None:
        return
    records = sorted((out/pending['evaluation_dir']/'attempts').glob('*/record.json'))
    if len(records) > 1:
        raise ValueError('more than one attempt for one paid slot')
    record = read_json(records[0]) if records else dict(
        status='interrupted', metrics={}, elapsed_seconds=pending['timeout'], cache_hit=False)
    pending['record'] = record
    pending['recovered_after_interrupt'] = True
    pending['accepted'] = record['status'] == 'success' and score(record) < score(state['best_record'])
    state['elapsed_seconds'] = max(state['elapsed_seconds'],
        pending.get('active_elapsed_at_start', state['elapsed_seconds']) +
        record.get('elapsed_seconds', pending['timeout']))
    state['calls'].append(pending)
    if record['status'] == 'success' and score(record) < score(state['best_record']):
        state['best_record'] = record
        state.pop('active_stream', None)


def run_arm(seed_result, family, out, budget=6, seconds=180, timeout=60,
            generation_seconds=12, batch_deadline=float('inf')):
    if family not in FAMILIES or budget < 1 or min(seconds, timeout, generation_seconds) <= 0:
        raise ValueError('invalid arm options')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    path = out/'summary.json'
    seed = seed_result['seed']
    state = read_json(path) if path.exists() else dict(
        seed_id=seed['id'], case=seed['case'], kind=seed['kind'], family=family,
        budget=budget, seconds=seconds, timeout=timeout, generation_timeout=generation_seconds,
        seed_record=seed_result['record'], best_record=seed_result['record'],
        selected_makespan=seed['selected_makespan'],
        selected_added_copy_bytes=seed['selected_added_copy_bytes'],
        calls=[], generations=[], skipped=[], elapsed_seconds=0., next_round=0,
        complete=False, scope='warm diagnostic; shared replay cost reported separately')
    if (state['seed_id'], state['family'], state['budget'], state['seconds'], state['timeout'],
            state['generation_timeout']) != (seed['id'], family, budget, seconds, timeout, generation_seconds):
        raise ValueError('cannot resume a different arm protocol')
    if state['complete']:
        return state
    _recover_pending(state, out)
    if not seed_result['verified']:
        state.update(complete=True, stop_reason='seed_unverified')
        atomic_json(path, state)
        return state
    carried = state['elapsed_seconds']
    start = time.monotonic()
    ir = GraphIR.from_path(DATA/(seed['case']+'.json'))
    seen = {exact_signature(read_json(state['seed_record']['plan_path']))}
    seen.update(c['signature'] for c in state['calls'])
    stream, parent, parent_plan, raw = None, None, None, None
    empty_streams = 0

    def remaining():
        return min(seconds-carried-(time.monotonic()-start), batch_deadline-time.time())

    def save():
        state['elapsed_seconds'] = carried + time.monotonic()-start
        state['logical_calls'] = len(state['calls']) + int(state.get('pending') is not None)
        atomic_json(path, state)

    save()
    finished = False
    try:
        while len(state['calls']) < budget and remaining() > 0 and empty_streams < 4:
            if stream is None:
                parent = state['best_record']
                parent_plan = read_json(parent['plan_path'])
                with gzip.open(parent['result_path'], 'rt') as f:
                    raw = json.load(f)
                assert_trace(ir, parent_plan, raw, parent)
                active = state.get('active_stream')
                if active:
                    if active['parent_record'] != parent['record_path']:
                        raise ValueError('resumed stream belongs to a different parent')
                    round_index = active['round']
                else:
                    round_index = state['next_round']
                    state['next_round'] += 1
                    state['active_stream'] = dict(round=round_index, parent_record=parent['record_path'])
                stream = proposals(ir, parent_plan, raw, family, round_index)
                yielded = 0
            t = time.monotonic()
            status, candidate, diagnostics, error = 'success', None, None, None
            try:
                with generation_limit(min(generation_seconds, remaining())):
                    candidate = next(stream)
                    validate_plan(ir, candidate['plan'])
                    if len(candidate['plan']['core_schedules']) != 5:
                        raise ValueError('candidate core count mismatch')
                    signature = exact_signature(candidate['plan'])
            except StopIteration as exc:
                status, diagnostics = 'exhausted', exc.value
            except TimeoutError as exc:
                status, error = 'generation_timeout', str(exc)
            except Exception as exc:
                status, error = 'generation_error', repr(exc)
            state['generations'].append(dict(round=round_index, parent_record=parent['record_path'],
                status=status, seconds=time.monotonic()-t, diagnostics=diagnostics, error=error))
            if candidate is None or status != 'success':
                stream.close()
                stream = None
                state.pop('active_stream', None)
                empty_streams += int(yielded == 0)
                if status != 'exhausted':
                    empty_streams += int(yielded != 0)
                save()
                continue
            if signature in seen:
                state['skipped'].append(dict(name=candidate['name'], reason='ordered_duplicate'))
                save()
                continue
            if remaining() <= 0:
                break
            seen.add(signature)
            yielded += 1
            empty_streams = 0
            relative = f'evaluations/call_{len(state["calls"])+1:03d}'
            trial = dict(name=candidate['name'], metadata=candidate.get('metadata', {}),
                         signature=signature, parent_record=parent['record_path'],
                         parent_makespan=score(parent)[0], round=round_index,
                         evaluation_dir=relative, timeout=min(timeout, remaining()),
                         active_elapsed_at_start=carried+time.monotonic()-start)
            state['pending'] = trial
            save()
            record = evaluate(ir.path, candidate['plan'], 1, out/relative,
                              timeout=trial['timeout'], config_path=DATA/'config.txt')
            trial['record'] = record
            state.pop('pending')
            trial['accepted'] = record['status'] == 'success' and score(record) < score(state['best_record'])
            state['calls'].append(trial)
            if trial['accepted']:
                state['best_record'] = record
                stream.close()
                stream = None
                state.pop('active_stream', None)
            save()
            print(json.dumps(dict(event='call', id=seed['id'], family=family,
                call=len(state['calls']), status=record['status'], accepted=trial['accepted'],
                best=score(state['best_record'])[0], selected=seed['selected_makespan']),
                ensure_ascii=False), flush=True)
        state['stop_reason'] = ('call_budget' if len(state['calls']) >= budget else
                                'time_budget' if remaining() <= 0 else 'candidate_exhaustion')
        finished = True
    except Exception as exc:
        state['stop_reason'], state['error'] = 'runner_error', repr(exc)
        finished = True
    finally:
        if stream is not None:
            stream.close()
        state['complete'] = finished
        if not finished:
            state['stop_reason'] = 'interrupted'
        save()
        state['scheduling_budget_overrun_seconds'] = max(0., state['elapsed_seconds']-seconds)
        atomic_json(path, state)
    return state


def _arm(job):
    return run_arm(*job)


def summarize(out, seeds, options, complete=False):
    rows = []
    for seed in seeds:
        for family in FAMILIES:
            p = out/'arms'/seed['id']/family/'summary.json'
            if not p.exists():
                continue
            state = read_json(p)
            best = state.get('best_record')
            good = best and best.get('status') == 'success'
            calls = state.get('calls', [])
            rows.append(dict(id=seed['id'], case=seed['case'], kind=seed['kind'], family=family,
                complete=state['complete'], calls=len(calls), failures=sum(c['record']['status']!='success' for c in calls),
                seed_makespan=seed['expected_makespan'], selected_makespan=seed['selected_makespan'],
                best_makespan=score(best)[0] if good else None,
                best_added_copy_bytes=score(best)[1] if good else None,
                beats_selected=good and score(best)<(seed['selected_makespan'],seed['selected_added_copy_bytes']),
                seconds=state['elapsed_seconds'], stop_reason=state.get('stop_reason'), summary=str(p)))
    write_csv(out/'comparison.csv', rows)
    progress = dict(complete=complete, arms_finished=sum(r['complete'] for r in rows), arms_planned=len(seeds)*2,
                    paid_candidate_calls=sum(r['calls'] for r in rows), max_candidate_calls=len(seeds)*2*options.budget,
                    rows=rows, scope='warm; shared seed replays and later acceptance replays are separate costs')
    atomic_json(out/'progress.json', progress)
    return progress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--budget', type=int, default=6)
    parser.add_argument('--seconds', type=float, default=180)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--seed-timeout', type=float, default=90)
    parser.add_argument('--batch-seconds', type=float, default=1200)
    parser.add_argument('--seeds-only', action='store_true', help='Verify starts, then stop before comparison arms')
    args = parser.parse_args()
    if args.workers < 1 or args.budget < 1 or not all(math.isfinite(x) and x>0 for x in
            (args.seconds,args.timeout,args.seed_timeout,args.batch_seconds)):
        parser.error('positive finite budgets required')
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    lock = (args.out/'run.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    source = read_json(args.inputs)
    seeds = source['seeds']
    options = dict(inputs=str(args.inputs.resolve()), workers=args.workers, budget=args.budget,
                   seconds=args.seconds, timeout=args.timeout, seed_timeout=args.seed_timeout,
                   batch_seconds=args.batch_seconds, generation_timeout=12,
                   families=list(FAMILIES), platform=platform.platform(),
                   seed_definitions=seeds,
                   observation='Official per-core op/task timeline; no recompilation; region uses task_release_fallback',
                   time_scope='Scheduling wall-time limit; evaluator setup/finalization may overrun and is reported',
                   description='Shared verified historical starts; same best-only controller; only family differs')
    protocol = args.out/'protocol.json'
    if protocol.exists() and read_json(protocol) != options:
        raise ValueError('output contains a different experiment protocol')
    atomic_json(protocol, options)
    deadline = time.time()+args.batch_seconds
    replays = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_replay, (s, str(args.out/'seeds'/s['id']), args.seed_timeout, deadline)) for s in seeds]
        for future in as_completed(futures):
            replays.append(future.result())
            atomic_json(args.out/'seed_replays.json', replays)
    replays.sort(key=lambda r: r['seed']['id'])
    if args.seeds_only:
        summarize(args.out, seeds, args)
        return
    jobs = [(r,f,str(args.out/'arms'/r['seed']['id']/f),args.budget,args.seconds,args.timeout,12,deadline)
            for r in replays for f in FAMILIES]
    summarize(args.out, seeds, args)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_arm, job) for job in jobs]
        for future in as_completed(futures):
            result = future.result()
            print(json.dumps(dict(event='arm_complete', id=result['seed_id'], family=result['family'],
                                  calls=len(result['calls']), reason=result['stop_reason']),ensure_ascii=False),flush=True)
            summarize(args.out,seeds,args)
    result = summarize(args.out,seeds,args,True)
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
