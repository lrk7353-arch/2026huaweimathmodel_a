"""Read-only P1 Task barrier witnesses from already paid official traces.

No official evaluation or compiler observation is run. A producer computation
ending does not imply COPY_OUT/DDR data readiness: every opportunity below is
an optimistic localization proxy, never a feasible saving or a lower bound.
"""
import argparse
from bisect import bisect_right
from collections import defaultdict
import gzip
import json
from pathlib import Path

from common_run import DATA, GraphIR, atomic_json, read_json, validate_plan, write_csv
from run_p1_relay_probe import assert_trace


LIMITATION = (
    'Observed Task release chain is not a counterfactual critical path. Compute '
    'completion does not establish COPY_OUT completion or DDR readability. '
    'The opportunity proxy substitutes the latest required producer compute '
    'completion for its whole Task end while keeping other observed releases '
    'fixed; it ignores new COPY costs, repartition overhead, memory and DDR '
    'contention. It cannot be claimed as feasible saved cycles.'
)


def analyze(ir, plan, raw, record, case, top=10):
    validate_plan(ir, plan)
    assert_trace(ir, plan, raw, record)
    mapping = {int(op): task for op, task in plan['node_to_subgraph'].items()}
    owner = {task: core for core, seq in enumerate(plan['core_schedules']) for task in seq}
    tasks = {task['task_id']: task for core in raw['per_core_timeline'] for task in core['tasks']}
    events = {event['op_id']: event for core in raw['per_core_timeline']
              for event in core['ops'] if event['op_id'] in mapping}
    task_compute = defaultdict(list)
    for op, task in mapping.items():
        task_compute[task].append((events[op]['end'], op))
    for values in task_compute.values():
        values.sort()
    task_compute_ends = {task: [end for end, _ in values] for task, values in task_compute.items()}
    dependencies = {(edge['source'], edge['target']) for edge in raw['task_dependencies']}
    if any(source not in tasks or target not in tasks for source, target in dependencies):
        raise ValueError('Unknown Task in official Task dependencies')
    predecessors = defaultdict(set)
    for source, target in dependencies:
        predecessors[target].add(source)
    previous = {target: source for seq in plan['core_schedules']
                for source, target in zip(seq, seq[1:])}
    cross_wait = raw['task_cross_core_wait_cycles']
    same_wait = raw['task_same_core_wait_cycles']
    core_available = {task: tasks[previous[task]]['end']+same_wait if task in previous else 0
                      for task in tasks}
    dependency_release = {
        target: {source: tasks[source]['end']+(cross_wait if owner[source] != owner[target] else 0)
                 for source in predecessors[target]}
        for target in tasks
    }
    latest_dependency = {task: max(dependency_release[task].values(), default=0) for task in tasks}
    observed_gate = {task: max(core_available[task], latest_dependency[task]) for task in tasks}
    residuals = {task: tasks[task]['start']-observed_gate[task] for task in tasks}
    if min(residuals.values(), default=0) < 0:
        raise ValueError('Official Task starts violate reconstructed Task release gates')

    # Keep all tied, tight Task-data/core-order predecessors of a makespan sink.
    # Gaps are reported instead of inventing a dependency through a non-tight gate.
    chain = set()
    pending = [task for task in tasks if tasks[task]['end'] == raw['makespan']]
    chain_edges = set()
    while pending:
        target = pending.pop()
        if target in chain:
            continue
        chain.add(target)
        start = tasks[target]['start']
        for source, release in dependency_release[target].items():
            if release == start:
                chain_edges.add((source, target, 'data'))
                pending.append(source)
        if target in previous and core_available[target] == start:
            source = previous[target]
            chain_edges.add((source, target, 'core'))
            pending.append(source)

    # O(compute dependence edges), not O(number of operations squared).
    pair_witnesses = {}
    edge_count = 0
    for target_op in ir.compute_ids:
        target_task = mapping[target_op]
        for source_op in ir.predecessors[target_op]:
            source_task = mapping[source_op]
            if source_task == target_task:
                continue
            edge_count += 1
            pair = source_task, target_task
            if pair not in dependencies:
                raise ValueError('GraphIR cross-Task edge absent from official Task dependency: '+str(pair))
            candidate = (events[source_op]['end'], -events[target_op]['start'], source_op, target_op)
            entry = pair_witnesses.setdefault(pair, dict(edges=0, latest=candidate))
            entry['edges'] += 1
            if candidate > entry['latest']:
                entry['latest'] = candidate

    rows = []
    for (source, target), pair in pair_witnesses.items():
        op_end, _, source_op, target_op = pair['latest']
        release = dependency_release[target][source]
        wait = cross_wait if owner[source] != owner[target] else 0
        other_release = max((value for task, value in dependency_release[target].items()
                             if task != source), default=0)
        optimistic_replacement_gate = max(core_available[target], other_release, op_end+wait)
        opportunity = max(0, observed_gate[target]-optimistic_replacement_gate)
        on_chain = target in chain
        latest = release == latest_dependency[target]
        core_masked = core_available[target] >= release
        other_masked = other_release >= release
        tail = tasks[source]['end']-op_end
        if tail < 0:
            raise ValueError('Producer computation ends after its Task')
        priority = (0 if on_chain and opportunity > 0 else
                    1 if opportunity > 0 else 2 if on_chain and tail > 0 else 3)
        after = bisect_right(task_compute_ends[source], op_end)
        last_compute_end = task_compute_ends[source][-1]
        rows.append(dict(
            case=case, baseline_makespan=raw['makespan'], priority_group=priority,
            source_task=source, target_task=target, source_core=owner[source], target_core=owner[target],
            source_op=source_op, target_op=target_op, pair_compute_edges=pair['edges'],
            source_task_compute_ops=len(task_compute[source]), target_task_compute_ops=len(task_compute[target]),
            producer_op_end=op_end, producer_task_end=tasks[source]['end'],
            consumer_op_start=events[target_op]['start'], consumer_task_start=tasks[target]['start'],
            compute_to_task_tail_cycles=tail, task_dependency_wait_cycles=wait,
            source_task_last_compute_end=last_compute_end,
            source_remaining_compute_tail_cycles=last_compute_end-op_end,
            source_post_compute_task_tail_cycles=tasks[source]['end']-last_compute_end,
            source_compute_ops_ending_later=len(task_compute[source])-after,
            source_later_compute_examples=';'.join(str(op) for _, op in task_compute[source][after:after+5]),
            producer_task_release=release, latest_dependency_release=latest_dependency[target],
            other_dependency_release=other_release, consumer_core_available=core_available[target],
            consumer_previous_core_task=previous.get(target),
            consumer_core_idle_to_start=max(0, tasks[target]['start']-core_available[target]),
            source_release_wait_after_core_ready=max(0, release-core_available[target]),
            core_ready_after_source_release=max(0, core_available[target]-release),
            consumer_on_observed_release_chain=on_chain, producer_on_observed_release_chain=source in chain,
            producer_is_latest_dependency=latest, core_masks_source_release=core_masked,
            other_dependency_masks_source_release=other_masked,
            data_edge_tight_on_observed_chain=(source, target, 'data') in chain_edges,
            unexplained_consumer_start_gap=residuals[target],
            optimistic_opportunity_proxy_cycles=opportunity,
            witness_scope='latest required compute across this Task pair; COPY_OUT readiness unknown',
        ))
    rows.sort(key=lambda row: (row['priority_group'], -row['optimistic_opportunity_proxy_cycles'],
                               -row['compute_to_task_tail_cycles'], row['source_task'], row['target_task']))
    selected = [dict(rank=index, **row) for index, row in enumerate(rows[:top], 1)]
    eligible = [row for row in rows if row['consumer_on_observed_release_chain']
                and row['optimistic_opportunity_proxy_cycles'] > 0]
    summary = dict(case=case, baseline_makespan=raw['makespan'], tasks=len(tasks),
                   compute_operations=len(ir.compute_ids), cross_task_compute_edges=edge_count,
                   witnessed_task_pairs=len(rows), official_task_dependency_pairs=len(dependencies),
                   observed_release_chain_tasks=len(chain),
                   tasks_with_unexplained_start_gap=sum(value > 0 for value in residuals.values()),
                   positive_compute_tail_pairs=sum(row['compute_to_task_tail_cycles'] > 0 for row in rows),
                   positive_unmasked_proxy_pairs=sum(row['optimistic_opportunity_proxy_cycles'] > 0 for row in rows),
                   critical_unmasked_proxy_pairs=len(eligible),
                   maximum_critical_proxy=max((row['optimistic_opportunity_proxy_cycles'] for row in eligible), default=0),
                   top=selected, limitation=LIMITATION)
    return selected, summary


def main():
    default = Path(__file__).resolve().parent/'P1接力实验_20260926'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=default/'run_v1')
    parser.add_argument('--out', type=Path, default=default)
    parser.add_argument('--top', type=int, default=10)
    args = parser.parse_args()
    if args.top < 1:
        parser.error('--top must be positive')
    results, summaries = [], []
    paths = sorted((args.run/'seeds').glob('*_selected/seed.json'))
    if not paths:
        raise ValueError('No selected seed replay records found')
    for path in paths:
        seed_result = read_json(path)
        if not seed_result.get('verified'):
            raise ValueError('Seed replay is not verified: '+str(path))
        seed, record = seed_result['seed'], seed_result['record']
        ir = GraphIR.from_path(DATA/(seed['case']+'.json'))
        plan = read_json(record['plan_path'])
        with gzip.open(record['result_path'], 'rt') as stream:
            raw = json.load(stream)
        rows, summary = analyze(ir, plan, raw, record, seed['case'], args.top)
        summary.update(seed_id=seed['id'], record_path=record['record_path'],
                       seed_replay_path=str(path.resolve()))
        results.extend(rows)
        summaries.append(summary)
    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(args.out/'关键屏障候选.csv', results)
    atomic_json(args.out/'关键屏障诊断.json', dict(
        source='run_v1 shared official replays of original selected seeds; 2026-09-26',
        scope='read-only trace localization; no evaluator/compiler calls; no guaranteed improvement',
        limitation=LIMITATION, cases=summaries))
    print(json.dumps([{key: value for key, value in summary.items()
                       if key not in ('top', 'limitation', 'record_path', 'seed_replay_path')}
                      for summary in summaries], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
