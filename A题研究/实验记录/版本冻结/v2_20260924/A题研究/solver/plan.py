"""Plan construction and structural validation, without executing evaluators."""
from collections import deque


def _positive_cores(num_cores):
    if type(num_cores) is not int or num_cores < 1:
        raise ValueError("num_cores must be a positive integer")


def validate_plan(ir, plan):
    """Raise ValueError if invalid; return True otherwise.

    Includes compute coverage, quotient DAG and the union of quotient edges
    with same-core adjacent-subgraph order. P2/P3's compiled Pipe/memory/COPY
    execution graph MUST still be checked by the official evaluator.
    """
    if not isinstance(plan, dict) or set(plan) != {"node_to_subgraph", "core_schedules"}:
        raise ValueError("plan must contain exactly the two official fields")
    raw = plan["node_to_subgraph"]
    if not isinstance(raw, dict):
        raise ValueError("node_to_subgraph must be an object")
    mapping = {}
    for node, sg in raw.items():
        if not isinstance(node, str) or not node.isascii() or not node.isdecimal():
            raise ValueError("mapping keys must be decimal op-id strings")
        ident = int(node)
        if ident in mapping:
            raise ValueError("duplicate integer op id")
        if type(sg) is not int or sg < 0:
            raise ValueError("subgraph ids must be nonnegative integers")
        mapping[ident] = sg
    if set(mapping) != set(ir.compute_ids):
        raise ValueError("mapping must exactly cover non-COPY ops")
    schedules = plan["core_schedules"]
    if not isinstance(schedules, list) or not schedules:
        raise ValueError("core_schedules must be a nonempty list")
    listed = []
    for order in schedules:
        if not isinstance(order, list):
            raise ValueError("each core schedule must be a list")
        if any(type(sg) is not int or sg < 0 for sg in order):
            raise ValueError("scheduled subgraph ids must be nonnegative integers")
        listed.extend(order)
    sgs = set(mapping.values())
    if len(listed) != len(set(listed)) or set(listed) != sgs:
        raise ValueError("each subgraph must be scheduled exactly once")
    adjacency = {sg: set() for sg in sgs}
    for op, next_ops in ir.successors.items():
        for nxt in next_ops:
            if mapping[op] != mapping[nxt]:
                adjacency[mapping[op]].add(mapping[nxt])
    for order in schedules:
        for a, b in zip(order, order[1:]):
            adjacency[a].add(b)
    indegree = {sg: 0 for sg in sgs}
    for children in adjacency.values():
        for child in children:
            indegree[child] += 1
    queue = deque(sorted(sg for sg in sgs if indegree[sg] == 0))
    visited = 0
    while queue:
        sg = queue.popleft()
        visited += 1
        for child in sorted(adjacency[sg]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(sgs):
        raise ValueError("subgraph dependencies plus core order contain a cycle")
    return True


def component_groups(ir, assignment, num_cores, granularity, order=None):
    """Canonical core -> subgraph -> component-ID tuples, before large JSON creation."""
    _positive_cores(num_cores)
    if granularity not in ("per_core", "per_component"):
        raise ValueError("unknown granularity")
    if len(assignment) != len(ir.components):
        raise ValueError("assignment must cover components")
    if any(type(c) is not int or c < 0 or c >= num_cores for c in assignment):
        raise ValueError("invalid core assignment")
    order = list(range(len(ir.components))) if order is None else list(order)
    if sorted(order) != list(range(len(ir.components))):
        raise ValueError("component order must be a permutation")
    cores = [[] for _ in range(num_cores)]
    for cid in order:
        cores[assignment[cid]].append(cid)
    if granularity == "per_core":
        return tuple((tuple(sorted(cids)),) if cids else () for cids in cores)
    return tuple(tuple((cid,) for cid in cids) for cids in cores)


def plan_from_component_groups(ir, groups):
    mapping, schedules, sgid = {}, [], 0
    for core in groups:
        scheduled = []
        for members in core:
            scheduled.append(sgid)
            for cid in members:
                for op in ir.components[cid].nodes:
                    mapping[str(op)] = sgid
            sgid += 1
        schedules.append(scheduled)
    plan = {"node_to_subgraph": mapping, "core_schedules": schedules}
    validate_plan(ir, plan)
    return plan
