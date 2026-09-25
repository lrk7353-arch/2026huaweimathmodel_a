"""P1 bottleneck Task subdivision followed by complete Task reassignment.

Rankings are heuristics. Only fixed-plan necessary bounds may prune. Subdivision
uses the current plan, not case IDs; tensor groups undergo quotient-SCC repair.
"""
from collections import defaultdict

from common_run import validate_plan
from p1_task_refine import view, coalesce
from p1_selective import (topological_order, _phase_blocks, _toposort_blocks,
                          block_views, _assign, task_lower_bound)
from p1_convex_regions import _scc_coarsen
from p1_boundary_lower_bound import boundary_ddr_lower_bound


def capped_regions(ir, nodes, work_cap):
    """Preserve large tensor connections until a per-Pipe work cap is reached.

    This is a proposal, not a memory/acyclicity certificate. Caller repairs the
    full quotient DAG before assignment; repair may exceed the work cap.
    """
    nodes = set(nodes)
    parent = {o: o for o in nodes}
    work = {o: {ir.ops[o]['pipe']: max(1, ir.ops[o]['cycles'])} for o in nodes}
    def root(o):
        while parent[o] != o:
            parent[o] = parent[parent[o]]
            o = parent[o]
        return o
    producers, consumers = defaultdict(list), defaultdict(list)
    for e in ir.graph['edges']:
        if e['source'] in nodes: producers[e['target']].append(e['source'])
        if e['target'] in nodes: consumers[e['source']].append(e['target'])
    links = []
    for t in producers.keys() & consumers.keys():
        for a in producers[t]:
            for b in consumers[t]: links.append((-ir.tensors[t]['size'], a, b))
    for _, a, b in sorted(links):
        a, b = root(a), root(b)
        if a == b: continue
        merged = {p: work[a].get(p, 0) + work[b].get(p, 0) for p in work[a].keys() | work[b].keys()}
        if max(merged.values()) <= work_cap:
            parent[b] = a
            work[a] = merged
    groups = defaultdict(list)
    for o in sorted(nodes): groups[root(o)].append(o)
    return list(groups.values())


def generate(ir, plan, raw, cores, limit=24, round_index=0):
    validate_plan(ir, plan)
    if cores != len(plan['core_schedules']) or limit < 1:
        raise ValueError('invalid core count or candidate limit')
    mapping, owner, nodes, _, _ = view(ir, plan)
    tasks = {e['task_id']: e for c in raw['per_core_timeline'] for e in c['tasks']}
    if set(tasks) != set(nodes): raise ValueError('P1 trace does not match Task IDs')
    for c in raw['per_core_timeline']:
        if [e['task_id'] for e in c['tasks']] != plan['core_schedules'][c['core_id']]:
            raise ValueError('P1 trace/plan schedule mismatch')
    ordered = topological_order(ir, 'stable_id')
    heavy = sorted((s for s in nodes if len(nodes[s]) >= 4),
                   key=lambda s: (-tasks[s]['duration'], -len(nodes[s]), s))[:4]
    targets = heavy[:1] if round_index % 2 == 0 else heavy[:2]
    ideal = max(ir.total_work_m, ir.total_work_v, 1) / cores
    specs = [('phase', b) for b in (2, 4, 8, 16)]
    specs += [('tensor_cap', scale) for scale in (.25, .5, 1., 2.)]
    candidates, rejected, seen = [], [], set()
    import json
    seen.add(json.dumps(plan, separators=(',', ':')))
    for kind, scale in specs:
        blocks = []
        for s, members in nodes.items():
            if s not in targets:
                blocks.append(members)
            elif kind == 'phase':
                member_set = set(members)
                local = [o for o in ordered if o in member_set]
                blocks.extend(_phase_blocks(ir, members, local, scale))
            else:
                blocks.extend(capped_regions(ir, members, max(1, ideal * scale)))
        blocks, repair = _scc_coarsen(ir, blocks, ordered)
        blocks = _toposort_blocks(ir, blocks, ordered)
        bview = block_views(ir, blocks)
        for method in ('eft', 'balanced'):
            new, proxy = _assign(ir, blocks, bview, cores, cores, method)
            # Joining adjacent tiny Tasks can remove the extra Task-start cost;
            # both the unmerged and merged candidate retain the new assignment.
            variants = [('raw', new), ('merge8', coalesce(ir, new, 8))]
            for merge, value in variants:
                identity = json.dumps(value, separators=(',', ':'))
                if identity in seen: continue
                seen.add(identity)
                validate_plan(ir, value)
                bound = task_lower_bound(ir, value)['value']
                ddr = boundary_ddr_lower_bound(ir, value)
                new_owner = {s: c for c, seq in enumerate(value['core_schedules']) for s in seq}
                moved = sum(new_owner[s] != owner[mapping[int(o)]] for o, s in value['node_to_subgraph'].items())
                candidates.append(dict(name=f'p1_joint_r{round_index}_{kind}{scale}_{method}_{merge}', plan=value,
                    metadata=dict(family=kind, scale=scale, assignment=method, merge=merge,
                        target_tasks=targets, moved_ops=moved, proxy=proxy,
                        task_bound=bound, ddr_bound=ddr['lower_bound'],
                        lower_bound=max(bound, ddr['lower_bound']),
                        task_count=sum(map(len, value['core_schedules'])),
                        mandatory_boundary_bytes=ddr['mandatory_boundary']['total_bytes'],
                        repair=repair)))
    # Interleave mechanisms so a biased proxy cannot suppress one entire family.
    groups = defaultdict(list)
    for c in candidates: groups[c['metadata']['family']].append(c)
    for group in groups.values():
        group.sort(key=lambda c: (c['metadata']['lower_bound'], c['metadata']['proxy'], c['name']))
    result = [g[i] for i in range(max(map(len, groups.values()), default=0))
              for g in groups.values() if i < len(g)]
    diag = dict(targets=[dict(task=s, ops=len(nodes[s]), duration=tasks[s]['duration'],
                             fraction=tasks[s]['duration']/max(1,raw['makespan'])) for s in heavy],
                generated=len(candidates), selected=min(limit,len(result)), rejected=rejected,
                bound_scope='fixed proposed plan only; not global optimum')
    return result[:limit], diag
