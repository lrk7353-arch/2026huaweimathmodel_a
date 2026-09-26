"""Auditable two-level P1 repair using one shared construction deadline.

Only active blocks may contract. Every contraction checks the entire dependency
graph including fixed outside-core order. The coarse solution is explicitly
projected to the original fine partition before a boundary-only joint repair.
Official evaluation, not the analytical proxy, decides whether to accept a plan.
"""
from collections import defaultdict
import heapq
import math
import time

from common_run import validate_plan


def _topological(preds):
    """Return a stable topological order or reject any quotient/order cycle."""
    successors = [set() for _ in preds]
    degree = []
    for child, parents in enumerate(preds):
        parents = set(parents)
        if child in parents:
            raise ValueError('self edge in block graph')
        degree.append(len(parents))
        for parent in parents:
            successors[parent].add(child)
    ready = [bid for bid, count in enumerate(degree) if count == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        bid = heapq.heappop(ready)
        order.append(bid)
        for child in sorted(successors[bid]):
            degree[child] -= 1
            if degree[child] == 0:
                heapq.heappush(ready, child)
    if len(order) != len(preds):
        raise ValueError('contraction conflicts with dependency/outside core order')
    return order


def _quotient(preds, groups):
    """Construct and validate the *full* quotient, not just the active region."""
    mapping = {fine: coarse for coarse, group in enumerate(groups) for fine in group}
    if set(mapping) != set(range(len(preds))):
        raise ValueError('coarsening must cover every fine block exactly once')
    if sum(map(len, groups)) != len(preds):
        raise ValueError('fine block appears in multiple coarse groups')
    result = [set() for _ in groups]
    for child, parents in enumerate(preds):
        for parent in parents:
            if mapping[parent] != mapping[child]:
                result[mapping[child]].add(mapping[parent])
    order = _topological(result)
    return mapping, result, order


def coarsen_groups(model, deadline=float('inf')):
    """Pair active adjacent blocks until roughly half remain, preserving barriers.

    Fine endpoints of the specifically observed early-release witness remain
    singleton groups. This prevents appending an unrelated tail to that producer
    as well as prevents erasing the producer-consumer Task boundary itself.
    """
    started = time.perf_counter()
    blocks, preds = model['blocks'], model['preds']
    active = set(model['active']) - set(model['fixed'])
    metadata = model.get('metadata', {})
    protected_pairs = [tuple(map(int, pair)) for pair in metadata.get('protected_pairs', [])]
    protected = {bid for pair in protected_pairs for bid in pair}
    protected.update(map(int, metadata.get('protected_singletons', [])))
    groups = [[bid] for bid in range(len(blocks))]
    target = max(2, min(24, math.ceil(len(active) / 2)))
    initial = len(active)
    merge_log, rejected = [], 0
    _topological(preds)
    # Durations influence balanced pairing only. Coarse durations are recomputed
    # from the resulting complete partition by model_with_blocks.
    durations = model['durations']
    while sum(any(bid in active for bid in group) for group in groups) > target:
        if time.perf_counter() >= deadline:
            break
        mapping, qpreds, _ = _quotient(preds, groups)
        eligible = {index for index, group in enumerate(groups)
                    if set(group) <= active and not (set(group) & protected)}
        options = []
        for child in eligible:
            for parent in qpreds[child] & eligible:
                left, right = groups[parent], groups[child]
                total = sum(durations[bid] for bid in left + right)
                # Preserve a reasonably balanced hierarchy rather than repeatedly
                # swallowing all active nodes into one growing chain.
                key = (len(left) + len(right), total, parent, child)
                options.append((key, parent, child))
        merged = False
        for _, parent, child in sorted(options):
            if time.perf_counter() >= deadline:
                break
            joined = sorted(groups[parent] + groups[child])
            proposal = [group for i, group in enumerate(groups) if i not in (parent, child)]
            proposal.append(joined)
            try:
                _, _, order = _quotient(preds, proposal)
            except ValueError:
                rejected += 1
                continue
            merge_log.append({'left': groups[parent][:], 'right': groups[child][:],
                              'joined': joined[:]})
            groups = [proposal[index] for index in order]
            merged = True
            break
        if not merged:
            break
    return groups, {
        'fine_active_blocks': initial,
        'coarse_active_blocks': sum(any(bid in active for bid in group) for group in groups),
        'target_coarse_active_blocks': target,
        'protected_pairs': [list(pair) for pair in protected_pairs],
        'protected_singletons': sorted(protected),
        'contractions': merge_log, 'rejected_cyclic_contractions': rejected,
        'coarsening_seconds': time.perf_counter() - started,
    }


def _block_assignment(plan, blocks):
    task_owner = {task: core for core, tasks in enumerate(plan['core_schedules']) for task in tasks}
    result = {}
    for bid, block in enumerate(blocks):
        tasks = {plan['node_to_subgraph'][str(op)] for op in block}
        if len(tasks) != 1:
            raise ValueError('solver split a prescribed block')
        result[bid] = task_owner[next(iter(tasks))]
    return result


def project_solution(ir, fine_model, coarse_model, coarse_plan):
    """Expand coarse ownership and per-core order into an explicit legal fine plan."""
    fine_blocks, coarse_blocks = fine_model['blocks'], coarse_model['blocks']
    op_to_coarse = {op: bid for bid, block in enumerate(coarse_blocks) for op in block}
    fine_to_coarse = {}
    for bid, block in enumerate(fine_blocks):
        coarse_ids = {op_to_coarse[op] for op in block}
        if len(coarse_ids) != 1:
            raise ValueError('coarse block split a fine block')
        fine_to_coarse[bid] = next(iter(coarse_ids))
    coarse_to_fine = defaultdict(list)
    fine_topo = _topological(fine_model['preds'])
    for bid in fine_topo:
        coarse_to_fine[fine_to_coarse[bid]].append(bid)
    task_to_coarse = {}
    for bid, block in enumerate(coarse_blocks):
        tasks = {coarse_plan['node_to_subgraph'][str(op)] for op in block}
        if len(tasks) != 1:
            raise ValueError('coarse solver returned an unexpected partition')
        task = next(iter(tasks))
        if task in task_to_coarse:
            raise ValueError('coarse solver merged prescribed blocks')
        task_to_coarse[task] = bid
    schedules = [[fine for task in tasks
                  for fine in coarse_to_fine[task_to_coarse[task]]]
                 for tasks in coarse_plan['core_schedules']]
    op_to_fine = {op: bid for bid, block in enumerate(fine_blocks) for op in block}
    projection = {
        'node_to_subgraph': {op: op_to_fine[int(op)]
                             for op in fine_model['incumbent']['node_to_subgraph']},
        'core_schedules': schedules,
    }
    validate_plan(ir, projection)
    owners = _block_assignment(projection, fine_blocks)
    if any(owners[bid] != core for bid, core in fine_model['fixed'].items()):
        raise ValueError('coarse projection moved a fixed outside block')
    return projection, fine_to_coarse


def refinement_model(model, projection, fine_to_coarse):
    """Freeze interiors at inherited owners; reopen a bounded coarse-boundary halo."""
    active = set(model['active']) - set(model['fixed'])
    owner = _block_assignment(projection, model['blocks'])
    # A coarse frontier touches both active-to-active and active-to-outside
    # dependencies. The protected witness always stays in the movable halo.
    boundary_weight = defaultdict(int)
    for child, parents in enumerate(model['preds']):
        for parent in parents:
            if fine_to_coarse[parent] != fine_to_coarse[child]:
                if parent in active:
                    boundary_weight[parent] += 1
                if child in active:
                    boundary_weight[child] += 1
    witnesses = {bid for pair in model.get('metadata', {}).get('protected_pairs', [])
                 for bid in pair if bid in active}
    limit = min(len(active), max(len(witnesses), 2, math.ceil(len(active) / 2)))
    boundary = set(witnesses)
    ranked = sorted(active, key=lambda bid: (-boundary_weight[bid], -model['durations'][bid], bid))
    for bid in ranked:
        if len(boundary) >= limit:
            break
        if boundary_weight[bid] > 0:
            boundary.add(bid)
    fixed = dict(model['fixed'])
    fixed.update({bid: owner[bid] for bid in active - boundary})
    preds = [set(parents) for parents in model['preds']]
    # Inherit the relative core order of all preserved interiors/outside blocks.
    # Movable halo blocks remain free to change owner and position jointly.
    inherited_edges = []
    for schedule in projection['core_schedules']:
        preserved = [bid for bid in schedule if bid in fixed]
        for first, second in zip(preserved, preserved[1:]):
            if first not in preds[second]:
                inherited_edges.append([first, second])
            preds[second].add(first)
    order = _topological(preds)
    updated = dict(model)
    updated.update(fixed=fixed, active=sorted(boundary), preds=preds,
                   preferred_owner=owner)
    updated['metadata'] = dict(model.get('metadata', {}), hierarchy_refinement=True)
    preferred_order = [bid for schedule in projection['core_schedules'] for bid in schedule]
    # Preferred order is a tie-break/rank only; precedence is enforced by preds.
    preferred = {'owner': owner, 'order': preferred_order}
    diagnostics = {'refinement_active_blocks': sorted(boundary),
                   'projected_fixed_active_blocks': sorted(active - boundary),
                   'inherited_core_order_edges': inherited_edges,
                   'refinement_topological_count': len(order)}
    return updated, preferred, diagnostics


def solve_multilevel(ir, model, deadline=float('inf'), width=8):
    """Coarsen, solve, project, repair within the caller's single deadline.

    Both the projected plan and the boundary-repaired plan are complete, freshly
    scored candidates. Returning the projection when refinement times out is an
    explicit outcome, not a claim that fine repair completed.
    """
    from p1_joint_frontier import model_with_blocks, score_plan, solve_beam
    started = time.perf_counter()
    if not math.isfinite(deadline):
        deadline = started + 20.0
    if deadline <= started:
        raise TimeoutError('multilevel construction deadline already elapsed')
    groups, diagnostics = coarsen_groups(model, deadline)
    if not diagnostics['contractions']:
        raise ValueError('no legal multilevel contraction; hierarchy was not constructed')
    fine_order = _topological(model['preds'])
    rank = {bid: index for index, bid in enumerate(fine_order)}
    coarse_blocks = [[op for bid in sorted(group, key=rank.get) for op in model['blocks'][bid]]
                     for group in groups]
    coarse_model = model_with_blocks(ir, model, coarse_blocks)
    now = time.perf_counter()
    if now >= deadline:
        raise TimeoutError('multilevel deadline elapsed during coarse model construction')
    # One total budget: coarse receives 40% of the time still available, with
    # the rest reserved for expansion, fine repair, validation and final scoring.
    coarse_deadline = now + .40 * (deadline - now)
    coarse_plan, coarse_diagnostics = solve_beam(ir, coarse_model, width=width,
                                                deadline=coarse_deadline)
    projection, fine_to_coarse = project_solution(ir, model, coarse_model, coarse_plan)
    projected_score = score_plan(ir, projection)
    repair_model, preferred, repair_diagnostics = refinement_model(model, projection, fine_to_coarse)
    result, chosen, repair_trace = projection, 'coarse_projection', {}
    refined_score = None
    try:
        if time.perf_counter() >= deadline:
            raise TimeoutError('no remaining shared time for fine repair')
        refined, repair_trace = solve_beam(ir, repair_model, width=width,
                                           deadline=deadline, preferred=preferred)
        validate_plan(ir, refined)
        refined_score = score_plan(ir, refined)
        if refined_score['proxy'] <= projected_score['proxy']:
            result, chosen = refined, 'boundary_refinement'
    except TimeoutError as error:
        repair_trace = {'status': 'deadline', 'message': str(error)}
    validate_plan(ir, result)
    final_owner = _block_assignment(result, model['blocks'])
    projected_owner = _block_assignment(projection, model['blocks'])
    original_task_core = {task: core for core, tasks in enumerate(model['incumbent']['core_schedules'])
                          for task in tasks}
    original_op_owner = {int(op): original_task_core[task]
                         for op, task in model['incumbent']['node_to_subgraph'].items()}
    diagnostics.update(repair_diagnostics)
    diagnostics.update({
        'method': 'two_level_coarsen_project_boundary_repair',
        'fine_total_blocks': len(model['blocks']), 'coarse_total_blocks': len(coarse_model['blocks']),
        'fine_to_coarse': {str(bid): coarse for bid, coarse in fine_to_coarse.items()},
        'projected_core_schedules': projection['core_schedules'],
        'projected_owners': {str(bid): core for bid, core in projected_owner.items()},
        'coarse_solver': coarse_diagnostics, 'fine_solver': repair_trace,
        'selected_stage': chosen, 'projection_score': projected_score,
        'refinement_score': refined_score,
        'moved_fine_blocks_from_projection': sum(final_owner[bid] != projected_owner[bid]
                                                 for bid in final_owner),
        'moved_operations_from_incumbent': sum(final_owner[bid] != original_op_owner[op]
                                               for bid, block in enumerate(model['blocks']) for op in block),
        'construction_seconds': time.perf_counter() - started,
        'shared_deadline_overrun_seconds': max(0., time.perf_counter() - deadline),
        'budget_scope': 'one caller deadline for contraction, coarse solve, projection, and fine repair',
    })
    return result, diagnostics
