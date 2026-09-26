"""Bounded warm-start mechanism comparison for the last competition evening.

The official evaluator is unmodified. Generation, attempts, interruptions and
fresh independent acceptance are recorded. This is not a cold-start score.
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
import traceback

from common_run import DATA, GraphIR, atomic_json, evaluate, read_json, score, validate_plan, write_csv
from persistent_budget import exact_signature
from persistent_search import generation_limit
from run_p1_relay_probe import assert_trace
from accept_p1_relay_gains import deterministic_metrics


HERE = Path(__file__).resolve().parent
ROOT = HERE/'P1多尺度联合优化_20260926'
METHODS = ('legacy', 'beam_small', 'cpsat_small', 'beam_large', 'multilevel')
AVAILABLE_METHODS = METHODS + ('cpsat_warm', 'cpsat_intact')
CASES = ('case_047', 'case_075', 'case_085')


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, set):
        return [clean(v) for v in sorted(value)]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def prepare(out):
    """Refresh localization on current verified winners, without evaluation."""
    from p1_relay_barrier_diagnostic import analyze
    base = HERE/'P1接力实验_20260926'
    seeds, rows, diagnostics = [], [], []
    for case in CASES:
        if case == 'case_085':
            entry = read_json(base/'run_v1/seeds/case_085_selected/seed.json')
            if not entry.get('verified'):
                raise ValueError('unverified initial plan')
            record = entry['record']
        else:
            entry = read_json(base/'独立复评与新增方案'/(case+'.verification.json'))
            if not entry.get('accepted') or not entry.get('deterministic_metrics_match'):
                raise ValueError('unverified latest plan')
            record = entry['replay_record']
        plan = read_json(record['plan_path'])
        ir = GraphIR.from_path(DATA/(case+'.json'))
        with gzip.open(record['result_path'], 'rt') as handle:
            raw = json.load(handle)
        found, diag = analyze(ir, plan, raw, record, case, top=12)
        rows.extend(found); diagnostics.append(diag)
        path = out/'inputs'/(case+'.json')
        if path.exists() and exact_signature(read_json(path)) != exact_signature(plan):
            raise ValueError('input has changed; choose a fresh output directory')
        atomic_json(path, plan)
        seeds.append(dict(case=case, plan_path=str(path), record=record,
                          expected_score=list(score(record)), trace_path=record['result_path']))
    result = dict(seeds=seeds, source='Latest independently verified warm plans',
                  official_calls=0, created_local=time.strftime('%Y-%m-%d %H:%M:%S %Z'))
    atomic_json(out/'inputs.json', result)
    atomic_json(out/'最新关键屏障.json', clean(diagnostics))
    write_csv(out/'最新关键屏障.csv', rows)
    return result


def warm_modules():
    started = time.monotonic()
    import p1_joint_frontier
    import p1_joint_multilevel
    import p1_joint_cpsat
    from ortools.sat.python import cp_model
    return time.monotonic()-started


def regional_coalesce(ir, plan, model):
    """Apply only legal coalesce groups wholly inside the released region."""
    from p1_task_refine import coalesce
    merged = coalesce(ir, plan, 8)
    active_ops = {op for b in model['active'] for op in model['blocks'][b]}
    groups = {}
    for op, representative in merged['node_to_subgraph'].items():
        groups.setdefault(representative, []).append(int(op))
    witness = model.get('metadata', {}).get('witness') or {}
    protected = {witness[k] for k in ('source_op', 'target_op') if k in witness}
    allowed = {g for g, ops in groups.items() if set(ops) <= active_ops
               and not (len(protected)==2 and protected <= set(ops))}
    remap = {}
    for op, old in plan['node_to_subgraph'].items():
        group = merged['node_to_subgraph'][op]
        remap[old] = group if group in allowed else old
    schedules = []
    for seq in plan['core_schedules']:
        result = []
        for old in seq:
            new = remap[old]
            if not result or result[-1] != new:
                result.append(new)
        schedules.append(result)
    result = dict(node_to_subgraph={op: remap[old] for op, old in plan['node_to_subgraph'].items()},
                  core_schedules=schedules)
    validate_plan(ir, result)
    return result


def preserve_intact_task_ids(ir, original, proposed):
    """Keep original IDs for unchanged Task memberships, including no-op moves.

    We do not assume renumbering is simulator-invariant. Instead we explicitly
    generate the intended original labels, so an unchanged plan is an exact
    duplicate and consumes no new evaluation slot.
    """
    groups = {}
    for op, task in proposed['node_to_subgraph'].items():
        groups.setdefault(task, set()).add(original['node_to_subgraph'][op])
    if any(len(old) != 1 for old in groups.values()):
        raise ValueError('intact candidate changed original Task membership')
    remap = {task: next(iter(old)) for task, old in groups.items()}
    if len(set(remap.values())) != len(remap):
        raise ValueError('intact candidate split an original Task')
    result = dict(node_to_subgraph={op: remap[task] for op, task in proposed['node_to_subgraph'].items()},
                  core_schedules=[[remap[t] for t in seq] for seq in proposed['core_schedules']])
    validate_plan(ir, result)
    return result


def candidates(ir, plan, raw, method, seconds):
    """Same final-plan proxy and same raw/coalesce postprocessor for new arms."""
    from p1_joint_frontier import build_problem, solve_beam, score_plan
    started = time.perf_counter()
    deadline = started+seconds
    result, diagnostics = [], {}
    if method == 'legacy':
        from p1_joint_regions import generate
        options, diagnostics = generate(ir, plan, raw, len(plan['core_schedules']), limit=64)
        for option in options:
            if time.perf_counter() >= deadline-0.1:
                break
            final_score = score_plan(ir, option['plan'])
            option['metadata']['old_pretransform_proxy'] = option['metadata'].get('proxy')
            option['metadata']['final_score'] = final_score
            option['metadata']['proxy'] = final_score['proxy']
            result.append(option)
    else:
        scope = 'small' if method in ('beam_small', 'cpsat_small', 'cpsat_warm', 'cpsat_intact') else 'large'
        model = build_problem(ir, plan, raw, scope=scope, deadline=deadline-2)
        original_warm = None
        if method == 'cpsat_intact':
            from p1_joint_frontier import model_with_blocks
            from p1_task_refine import view
            _, _, nodes, _, _ = view(ir, plan)
            model = model_with_blocks(ir, model, [nodes[t] for t in sorted(nodes)])
            original_warm = dict(node_to_subgraph={op: model['mapping'][int(op)]
                for op in plan['node_to_subgraph']}, core_schedules=[
                    [model['mapping'][nodes[t][0]] for t in seq] for seq in plan['core_schedules']])
            validate_plan(ir, original_warm)
            model['metadata'].update(active_count=len(model['active']), protected_pairs=[],
                partition='original incumbent Task boundaries retained',
                grouping_scope='same selected region, no forced fine splitting')
        diagnostics['model'] = model.get('metadata', {})
        diagnostics['active_blocks'] = len(model['active'])
        diagnostics['all_blocks'] = len(model['blocks'])
        if method in ('cpsat_small', 'cpsat_warm', 'cpsat_intact'):
            from p1_joint_cpsat import solve_cpsat
            if method in ('cpsat_warm', 'cpsat_intact'):
                warm_plan, warm_diag = solve_beam(ir, model, width=8,
                    deadline=min(deadline-2, time.perf_counter()+3))
                if original_warm is not None and score_plan(ir, original_warm)['proxy'] < score_plan(ir, warm_plan)['proxy']:
                    warm_plan = original_warm
                    warm_diag['chosen_original_incumbent'] = True
                diagnostics['counted_warm_beam'] = warm_diag
                proposed, solver_diag = solve_cpsat(ir, model, deadline=deadline-2,
                                                   seed=17, warm_plan=warm_plan)
            else:
                proposed, solver_diag = solve_cpsat(ir, model, deadline=deadline-2, seed=17)
        elif method == 'multilevel':
            from p1_joint_multilevel import solve_multilevel
            proposed, solver_diag = solve_multilevel(ir, model, deadline=deadline-2, width=8)
        else:
            proposed, solver_diag = solve_beam(ir, model, width=8, deadline=deadline-2)
        diagnostics['solver'] = solver_diag
        if proposed is not None:
            validate_plan(ir, proposed)
            if method == 'cpsat_intact':
                proposed = preserve_intact_task_ids(ir, plan, proposed)
                diagnostics['preserved_original_task_ids'] = True
            for name, candidate in [('raw', proposed), ('merge8_active', regional_coalesce(ir, proposed, model))]:
                if time.perf_counter() >= deadline:
                    break
                validate_plan(ir, candidate)
                final_score = score_plan(ir, candidate)
                result.append(dict(name=method+'_'+name, plan=candidate,
                                   metadata=dict(transform=name, proxy=final_score['proxy'],
                                                 final_score=final_score)))
    seen = {exact_signature(plan)}
    unique = []
    for candidate in sorted(result, key=lambda c: (c['metadata']['proxy'],
                            c['metadata'].get('transform') != 'raw', c['name'])):
        signature = exact_signature(candidate['plan'])
        if signature not in seen:
            seen.add(signature); unique.append(candidate)
    diagnostics.update(generation_seconds=time.perf_counter()-started,
                       generated=len(result), unique_nonparent=len(unique),
                       selection='up to 2 distinct full plans by recomputed proxy')
    return unique[:2], diagnostics


def recover_pending(state, out):
    pending = state.pop('pending', None)
    if pending is None:
        return
    records = list((out/pending['evaluation_dir']/'attempts').glob('*/record.json'))
    if len(records) > 1:
        raise ValueError('multiple paid attempts for one reserved slot')
    record = read_json(records[0]) if records else dict(status='interrupted', metrics={},
                elapsed_seconds=pending['timeout'], cache_hit=False)
    state['calls'].append(dict(**pending, record=record, recovered=True))
    state['elapsed_seconds'] = max(state.get('elapsed_seconds', 0),
            pending['elapsed_at_start']+record.get('elapsed_seconds', pending['timeout']))


def run_arm(job):
    seed, method, out_str, protocol, batch_deadline = job
    out = Path(out_str)
    out.mkdir(parents=True, exist_ok=True)
    path = out/'summary.json'
    start = time.monotonic()
    with (out/'arm.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            state = read_json(path)
            if state['protocol'] != protocol or state['seed_score'] != seed['expected_score']:
                raise ValueError('resume protocol/seed mismatch')
            if exact_signature(read_json(state['seed_record']['plan_path'])) != exact_signature(read_json(seed['plan_path'])):
                raise ValueError('resume seed plan identity changed')
            if state['complete']:
                return state
            recover_pending(state, out)
        else:
            state = dict(case=seed['case'], method=method, protocol=protocol,
                seed_score=seed['expected_score'], seed_record=seed['record'],
                complete=False, generation_complete=False, candidates=[], calls=[],
                elapsed_seconds=0, errors=[], best_record=seed['record'])
        elapsed_before = state['elapsed_seconds']
        def elapsed():
            return elapsed_before+time.monotonic()-start
        def remaining():
            return min(protocol['arm_seconds']-elapsed(), batch_deadline-time.time())
        def save():
            state['elapsed_seconds'] = elapsed()
            atomic_json(path, clean(state))
        try:
            state['module_setup_seconds'] = warm_modules()
            ir = GraphIR.from_path(DATA/(seed['case']+'.json'))
            plan = read_json(seed['plan_path'])
            validate_plan(ir, plan)
            with gzip.open(seed['trace_path'], 'rt') as handle:
                raw = json.load(handle)
            assert_trace(ir, plan, raw, seed['record'])
            if not state['generation_complete']:
                budget = min(protocol['generation_seconds'], remaining())
                if budget <= 0:
                    state['stop_reason'] = 'deadline_before_generation'
                else:
                    generation_started = time.monotonic()
                    try:
                        with generation_limit(budget):
                            selected, diag = candidates(ir, plan, raw, method, budget)
                        for i, candidate in enumerate(selected[:protocol['max_candidates']]):
                            candidate_path = out/'candidates'/f'{i+1:02d}.json'
                            atomic_json(candidate_path, candidate['plan'])
                            state['candidates'].append(dict(index=i, name=candidate['name'],
                                plan_path=str(candidate_path), metadata=clean(candidate['metadata'])))
                        state['generation'] = clean(diag)
                    except Exception as error:
                        state['errors'].append(dict(stage='generation', type=type(error).__name__,
                                                   error=str(error), traceback=traceback.format_exc()))
                    state['generation_wall_seconds'] = time.monotonic()-generation_started
                state['generation_complete'] = True
                save()
            used = {c['index'] for c in state['calls']}
            for candidate in state['candidates']:
                if candidate['index'] in used:
                    continue
                rem = remaining()
                if rem <= 0:
                    state['stop_reason'] = 'deadline_before_evaluation'; break
                evaluation_dir = 'evaluations/call_%02d' % (candidate['index']+1)
                pending = dict(**candidate, evaluation_dir=evaluation_dir,
                               timeout=min(protocol['worker_seconds'], rem), elapsed_at_start=elapsed())
                state['pending'] = pending
                save()
                value = read_json(candidate['plan_path'])
                record = evaluate(ir.path, value, 1, out/evaluation_dir,
                                  timeout=pending['timeout'], config_path=DATA/'config.txt')
                state['calls'].append(dict(**pending, record=record))
                state.pop('pending')
                if record['status'] == 'success' and score(record) < score(state['best_record']):
                    state['best_record'] = record
                save()
                print(json.dumps(dict(event='call', case=seed['case'], method=method,
                    name=candidate['name'], status=record['status'],
                    score=score(record) if record['status']=='success' else None), ensure_ascii=False), flush=True)
            # Recompute after recovering a paid attempt, too.
            state['best_record'] = min([seed['record']]+[c['record'] for c in state['calls']
                if c['record']['status']=='success'], key=score)
            state['complete'] = True
            state.setdefault('stop_reason', 'completed' if not state['errors'] else 'generation_error')
            save()
        except BaseException as error:
            state['last_interruption'] = dict(type=type(error).__name__, error=str(error))
            save()
            raise
    return state


def summarize(out, inputs, methods):
    rows, states = [], []
    for seed in inputs['seeds']:
        for method in methods:
            path = out/'arms'/seed['case']/method/'summary.json'
            if not path.exists():
                rows.append(dict(case=seed['case'], method=method, complete=False, status='not_started'))
                continue
            state = read_json(path); states.append(state)
            rows.append(dict(case=seed['case'], method=method, complete=state['complete'],
                status=state.get('stop_reason', 'running'), seed_time=seed['expected_score'][0],
                best_time=score(state['best_record'])[0], best_copy=score(state['best_record'])[1],
                candidates=len(state['candidates']), paid_calls=len(state['calls']),
                successes=sum(c['record']['status']=='success' for c in state['calls']),
                errors=len(state['errors']), generation_seconds=state.get('generation_wall_seconds'),
                elapsed_seconds=state['elapsed_seconds']))
    write_csv(out/'方法对照.csv', rows)
    calls = [dict(case=s['case'], method=s['method'], name=c['name'],
                  predicted=c['metadata'].get('proxy'), status=c['record']['status'],
                  makespan=score(c['record'])[0] if c['record']['status']=='success' else None,
                  added_copy=score(c['record'])[1] if c['record']['status']=='success' else None,
                  seconds=c['record'].get('elapsed_seconds'), record_path=c['record'].get('record_path'))
             for s in states for c in s['calls']]
    write_csv(out/'逐次候选.csv', calls)
    summary = dict(complete=all(r['complete'] for r in rows), arms=len(rows),
                   completed=sum(r['complete'] for r in rows), paid_calls=len(calls), rows=rows,
                   scope='Warm mechanism probe; different partition/feasible sets explicitly reported')
    atomic_json(out/'summary.json', summary)
    return summary


def accept(out, inputs, methods):
    results = []
    for seed in inputs['seeds']:
        acceptance_reference = seed.get('acceptance_reference_score', seed['expected_score'])
        pool = []
        for method in methods:
            path = out/'arms'/seed['case']/method/'summary.json'
            if not path.exists(): continue
            state = read_json(path)
            if not state['complete']: raise ValueError('unfinished arm cannot be accepted')
            pool.extend((method, call) for call in state['calls']
                        if call['record']['status']=='success' and score(call['record']) < tuple(acceptance_reference))
        if not pool: continue
        method, winner = min(pool, key=lambda value: score(value[1]['record']))
        path = out/'acceptance'/(seed['case']+'.json')
        if path.exists():
            saved = read_json(path)
            if saved['source_record_path'] != winner['record']['record_path']:
                raise ValueError('acceptance source changed; use fresh output directory')
            results.append(saved); continue
        plan = read_json(winner['record']['plan_path'])
        record = evaluate(DATA/(seed['case']+'.json'), plan, 1,
                          out/'acceptance/evaluations'/seed['case'], timeout=60, config_path=DATA/'config.txt')
        matched = record['status']=='success' and deterministic_metrics(record)==deterministic_metrics(winner['record'])
        if matched:
            matched = exact_signature(read_json(record['plan_path'])) == exact_signature(plan)
            matched = matched and all(record['hashes'][k] == winner['record']['hashes'][k]
                for k in ('graph_sha256', 'config_sha256', 'official_py_sha256'))
        exported = None
        if matched:
            exported = out/'verified_plans'/(seed['case']+'_p1_n5.json')
            atomic_json(exported, plan)
        saved = dict(case=seed['case'], method=method, accepted=matched,
                     seed_score=seed['expected_score'], candidate_score=list(score(winner['record'])),
                     acceptance_reference_score=acceptance_reference,
                     source_record_path=winner['record']['record_path'], replay_record=record,
                     plan_path=str(exported) if exported else None,
                     scope='Time-first alternative; COPY and simulated memory must be reported separately')
        atomic_json(path, saved); results.append(saved)
        print(json.dumps(dict(event='acceptance', case=seed['case'], accepted=matched,
                              candidate_score=saved['candidate_score']), ensure_ascii=False), flush=True)
    atomic_json(out/'acceptance/summary.json', dict(complete=True, rows=results, independent_calls=len(results)))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT/'run_v1')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--summary-only', action='store_true')
    parser.add_argument('--accept', action='store_true')
    parser.add_argument('--methods', nargs='+', choices=AVAILABLE_METHODS, default=list(METHODS))
    parser.add_argument('--cases', nargs='+', choices=CASES, default=list(CASES))
    parser.add_argument('--generation-seconds', type=float, default=20)
    parser.add_argument('--arm-seconds', type=float, default=180)
    parser.add_argument('--worker-seconds', type=float, default=60)
    parser.add_argument('--batch-seconds', type=float, default=1200)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--max-candidates', type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    for value in (args.generation_seconds, args.arm_seconds, args.worker_seconds, args.batch_seconds):
        if not math.isfinite(value) or value <= 0: parser.error('budgets must be positive finite')
    if args.workers not in (1, 2): parser.error('use one or two workers')
    out = args.out.resolve(); out.mkdir(parents=True, exist_ok=True)
    with (out/'batch.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        inputs = read_json(out/'inputs.json') if (out/'inputs.json').exists() else prepare(out)
        if args.prepare_only:
            print(json.dumps([dict(case=s['case'], score=s['expected_score']) for s in inputs['seeds']])); return
        inputs = dict(inputs, seeds=[s for s in inputs['seeds'] if s['case'] in args.cases])
        if not args.summary_only and not args.accept:
            protocol = dict(generation_seconds=args.generation_seconds, arm_seconds=args.arm_seconds,
                            worker_seconds=args.worker_seconds, version=1, max_candidates=args.max_candidates)
            jobs = [(seed, method, str(out/'arms'/seed['case']/method), protocol, time.time()+args.batch_seconds)
                    for seed in inputs['seeds'] for method in args.methods]
            started = time.monotonic()
            atomic_json(out/'run_info.json', dict(started_local=time.strftime('%Y-%m-%d %H:%M:%S %Z'),
                platform=platform.platform(), protocol=protocol, methods=args.methods, workers=args.workers))
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(run_arm, job): (job[0]['case'], job[1]) for job in jobs}
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        print(json.dumps(dict(event='arm', case=result['case'], method=result['method'],
                            calls=len(result['calls']), errors=result['errors'], best=score(result['best_record'])),
                            ensure_ascii=False), flush=True)
                    except Exception as error:
                        print(json.dumps(dict(event='arm_error', job=futures[future], error=repr(error)),
                                         ensure_ascii=False), flush=True)
                    summarize(out, inputs, args.methods)
            atomic_json(out/'batch_wall.json', dict(seconds=time.monotonic()-started))
        summary = summarize(out, inputs, args.methods)
        if args.accept:
            if not summary['complete']: raise ValueError('finish batch before independent acceptance')
            accept(out, inputs, args.methods)
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
