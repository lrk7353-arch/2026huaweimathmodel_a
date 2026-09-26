"""Apply verified FIFO-derived priorities, then repair compute-pipe gaps.

The existing cache module replays insertion-on-completion FIFO events exactly.
Its target criticality is still a heuristic. Both controls use the same atomic
encoding and fixed ownership, isolating the subsequent gap repair.
"""
import copy
import time

from p3_joint_reads import generate
from event_frontier import insertion_schedule,operation_plan
from unified_structure import Structure,owner_map


def candidates(ir,problem,cores,parent,raw,deadline=float('inf')):
    if problem!=3:raise ValueError('FIFO priorities require P3')
    source,diagnostics=generate(ir,parent,raw,cores,limit=4)
    structure=Structure(ir)
    for c in source:
        if time.monotonic()>=deadline:raise TimeoutError('cache-gap deadline')
        plan=c['plan'];order=list(map(int,plan['node_to_subgraph']));owners=owner_map(plan)
        meta=dict(family='cache_gap_link',source=c['metadata'],cache_diagnostics=diagnostics)
        yield dict(name=c['name']+'_atomic',plan=operation_plan(ir,order,owners,cores),
                   metadata=dict(meta,gap_repair=False))
        # insertion_schedule uses tail only as a ready-list/final tie priority.
        # Replace that priority in a private view; original cycles, tensors,
        # dependencies and the base Structure are untouched.
        priorities=copy.copy(structure)
        priorities.tail={o:len(order)-i for i,o in enumerate(order)}
        _,after,info=insertion_schedule(priorities,cores,fixed=owners,deadline=deadline)
        yield dict(name=c['name']+'_gap',plan=operation_plan(ir,after,owners,cores),
                   metadata=dict(meta,gap_repair=True,**info))
