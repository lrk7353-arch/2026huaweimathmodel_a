"""Obtain a paid official incumbent before admitting any P1 optimization.

No historical plan is read. Two deterministic safety layouts bound Task size;
the second has protected wall time and a call, rather than a late best-effort
fallback. This is a feasibility initializer, not a performance claim.
"""
from collections import deque
import math
import time
from pathlib import Path

from common_run import DATA, GraphIR, atomic_json, evaluate, score, validate_plan


POLICY_VERSION = 'incumbent_first_v1'


def safety_plan(ir, cores, chunk_size=128, serial=False):
    """Keep WCCs on one core; cut each into consecutive topological Tasks."""
    if type(cores) is not int or cores not in range(1, 6) or chunk_size < 1:
        raise ValueError('positive chunk size and cores 1..5 required')
    schedules = [[] for _ in range(cores)]
    mapping, loads, task = {}, [0] * cores, 0
    for component in ir.components:
        degree = {op: len(ir.predecessors[op]) for op in component.nodes}
        ready = deque(op for op in component.nodes if degree[op] == 0)
        ordered = []
        while ready:
            op = ready.popleft()
            ordered.append(op)
            for nxt in ir.successors[op]:
                degree[nxt] -= 1
                if degree[nxt] == 0:
                    ready.append(nxt)
        if len(ordered) != len(component.nodes):
            raise ValueError('cyclic component in initializer')
        core = 0 if serial else min(range(cores), key=lambda c: (loads[c], c))
        for start in range(0, len(ordered), chunk_size):
            schedules[core].append(task)
            for op in ordered[start:start+chunk_size]:
                mapping[str(op)] = task
            task += 1
        loads[core] += component.compute_work
    plan = dict(node_to_subgraph=mapping, core_schedules=schedules)
    validate_plan(ir, plan)
    return plan


def run(case, cores, out, budget, seconds, evaluation_timeout, evaluation_dir):
    if type(budget) is not int or budget < 1 or not all(
            math.isfinite(x) and x > 0 for x in (seconds, evaluation_timeout)):
        raise ValueError('positive finite call/time limits required')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    deadline = started + seconds
    # Up to 75% is protected for finding an incumbent. Once one exists,
    # unused initialization time is immediately returned to optimization.
    seed_seconds = min(180., .75 * seconds)
    seed_deadline = started + seed_seconds
    fallback_seconds = seed_seconds / 3 if budget >= 2 else 0.
    attempts = [('bounded_wcc_128', 128, False)]
    if budget >= 2:
        attempts.append(('bounded_serial_32', 32, True))
    calls, events, best = [], [], None
    policy = dict(version=POLICY_VERSION, initialization_seconds=seed_seconds,
        fallback_reserved_seconds=fallback_seconds, fallback_reserved_calls=int(budget>=2),
        optimization_evaluation_timeout=evaluation_timeout,
        primary_evaluation_cap_seconds=seed_seconds-fallback_seconds,
        initializer_uses_protected_cap=True,
        scope='same total budget; initializer cap may exceed optimization per-call cap')

    def save(done=False):
        result = dict(case=case, problem=1, cores=cores, policy=policy,
            complete=done, calls=calls, logical_calls=len(calls), best_record=best,
            status='success' if best else ('incumbent_unverified' if done else 'initializing'),
            events=events, elapsed_seconds=time.monotonic()-started,
            first_incumbent_seconds=next((c['finished_seconds'] for c in calls
                if c['record']['status']=='success'), None),
            stop_reason='incumbent_ready' if best else 'initialization_exhausted' if done else None)
        atomic_json(out/('summary.json' if done else 'progress.json'), result)
        return result

    save()
    try:
        from persistent_search import generation_limit
        with generation_limit(min(10., max(.001, seed_deadline-time.monotonic()))):
            ir = GraphIR.from_path(DATA/(case+'.json'))
    except Exception as exc:
        events.append(dict(phase='input', error=repr(exc)))
        return save(True)
    seen = set()
    for index, (name, chunk, serial) in enumerate(attempts):
        attempt_deadline = seed_deadline - (fallback_seconds if index == 0 else 0)
        remaining = attempt_deadline-time.monotonic()
        if remaining <= 0 or len(calls) >= budget:
            continue
        try:
            with generation_limit(min(10., remaining)):
                plan = safety_plan(ir, cores, chunk, serial)
            # Mapping order is significant to the official builder.
            signature = (tuple(plan['node_to_subgraph'].items()),
                         tuple(map(tuple, plan['core_schedules'])))
            if signature in seen:
                events.append(dict(phase=name, skipped='identical_safety_plan'))
                continue
            seen.add(signature)
        except Exception as exc:
            events.append(dict(phase=name, generation_error=repr(exc)))
            continue
        remaining = min(attempt_deadline, deadline)-time.monotonic()
        if remaining <= 0:
            continue
        try:
            record = evaluate(ir.path, plan, 1, evaluation_dir, timeout=remaining,
                              config_path=DATA/'config.txt')
        except Exception as exc:
            record = dict(status='wrapper_exception', error=repr(exc), cache_hit=False)
        calls.append(dict(name=name, phase='incumbent', record=record,
            requested_timeout_seconds=remaining, accepted=record['status']=='success',
            finished_seconds=time.monotonic()-started))
        if record['status'] == 'success':
            if (record.get('problem') != 1 or record['metrics']['num_cores'] != cores
                    or Path(record['graph_path']).stem != case):
                raise ValueError('official initializer record mismatch')
            best = record
            atomic_json(out/'best.plan.json', plan)
            # Persist the verified seed before returning to any optimizer.
            atomic_json(out.parent/'incumbent.plan.json', plan)
            atomic_json(out.parent/'incumbent.record.json', record)
            save()
            break
        save()
    return save(True)


if __name__ == '__main__':
    import argparse
    import json
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',type=int,required=True)
    parser.add_argument('--cores',type=int,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=240)
    args=parser.parse_args()
    result=run(f'case_{args.case:03d}',args.cores,args.out/'prefix',2,args.seconds,60,args.out/'evaluations')
    print(json.dumps({k:v for k,v in result.items() if k not in ('calls','best_record')},ensure_ascii=False))
