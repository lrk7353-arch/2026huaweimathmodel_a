"""Trace-backed P1 diagnostics. Static work/paths are clues, not speedup promises."""
from collections import Counter
from p1_task_refine import view
from p1_selective import topological_order


def diagnose(ir, plan, raw):
    mapping, owner, nodes, edges, pred = view(ir, plan)
    tasks = {t['task_id']: t for c in raw['per_core_timeline'] for t in c['tasks']}
    previous = {b: a for seq in plan['core_schedules'] for a, b in zip(seq, seq[1:])}
    terminal = max(tasks, key=lambda t: (tasks[t]['end'], t))
    chain, releases = [], {}
    current = terminal
    while current is not None:
        chain.append(current)
        choices = [(tasks[p]['end'] + (1000 if owner[p] != owner[current] else 0),
                    p, 'data') for p in pred[current]]
        if current in previous:
            p = previous[current]
            choices.append((tasks[p]['end'] + 100, p, 'core_order'))
        release, blocker, kind = max(choices, default=(0, None, 'source'))
        releases[current] = {'release': release, 'start': tasks[current]['start'],
                             'unexplained_start_gap': tasks[current]['start'] - release,
                             'blocker': blocker, 'kind': kind}
        current = blocker
    ends = {}
    for op in topological_order(ir, 'stable_id'):
        ends[op] = ir.ops[op]['cycles'] + max((ends[p] for p in ir.predecessors[op]), default=0)
    detail = []
    for task, members in nodes.items():
        wm = sum(ir.ops[o]['cycles'] for o in members if ir.ops[o]['pipe'] == 'PIPE_M')
        wv = sum(ir.ops[o]['cycles'] for o in members if ir.ops[o]['pipe'] == 'PIPE_V')
        detail.append({'task': task, 'core': owner[task], 'ops': len(members),
                       'work_m': wm, 'work_v': wv, **tasks[task]})
    return {'makespan': raw['makespan'], 'task_count': len(nodes),
            'core_ends': [max((t['end'] for t in c['tasks']), default=0) for c in raw['per_core_timeline']],
            'compute_dependency_path': max(ends.values(), default=0),
            'compute_work_m': ir.total_work_m, 'compute_work_v': ir.total_work_v,
            'final_blocker_chain': chain, 'release_witnesses': releases,
            'heavy_tasks': sorted(detail, key=lambda t: (-t['duration'], t['task'])),
            'warning': 'Trace releases explain Task-level blocking; duration also contains DDR/pipe/memory effects. '
                       'Contracted compute path and work do not establish achievable parallelism.'}


def regions(ir, plan, diagnostic, cap=8192, limit=2):
    """Whole old Tasks, expanded through data neighbors by observed duration."""
    _, _, nodes, edges, pred = view(ir, plan)
    duration = {x['task']: x['duration'] for x in diagnostic['heavy_tasks']}
    chain = diagnostic['final_blocker_chain']
    seeds = [diagnostic['heavy_tasks'][0]['task']]
    if chain:
        seeds.append(chain[0])
    result = []
    for seed in seeds:
        if len(nodes[seed]) > cap:
            continue  # Do not silently exceed the declared search envelope.
        selected, count = {seed}, len(nodes[seed])
        frontier = (edges[seed] | pred[seed]) - selected
        while frontier:
            options = [t for t in frontier if count + len(nodes[t]) <= cap]
            if not options:
                break
            task = min(options, key=lambda t: (-duration[t], t))
            selected.add(task)
            count += len(nodes[task])
            frontier = (frontier | edges[task] | pred[task]) - selected
        value = sorted(selected)
        if value not in result:
            result.append(value)
        if len(result) >= limit:
            break
    return result


def region_trace(raw, operations):
    """Measure original operation sets, so changed Task IDs remain comparable."""
    events, starts, ends, work = [], [], [], Counter()
    for core in raw['per_core_timeline']:
        intervals = []
        for op in core['ops']:
            if op['op_id'] in operations and op['pipe'] in ('PIPE_M', 'PIPE_V'):
                starts.append(op['start']); ends.append(op['end'])
                work[core['core_id']] += op['duration']
                intervals.append((op['start'], op['end']))
        # Union overlapping M/V intervals per core before counting active cores.
        merged = []
        for a, b in sorted(intervals):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for a, b in merged:
            events.extend(((a, 1), (b, -1)))
    active = peak = 0
    for _, delta in sorted(events):
        active += delta; peak = max(peak, active)
    return {'first_compute': min(starts, default=0), 'last_compute': max(ends, default=0),
            'peak_simultaneous_cores': peak, 'compute_cycles_by_core': dict(work)}
