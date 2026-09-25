"""Combine gap insertion with live-set ordering and input-region placement.

This uses the existing bounded lifetime and byte-delta neighborhoods without
recompiling a parent's official graph. The insertion order is the trust-region
reference. Shared-input adjacency is a proposal, not a predicted FIFO hit.
"""
from collections import defaultdict
import heapq
import time

from event_frontier import insertion_schedule, operation_plan
from p23_data_refine import lifetime_order, reuse_proposals
from unified_structure import Structure, owner_map


def input_window(structure, order, window=32):
    ir=structure.ir;rank={o:i for i,o in enumerate(order)}
    roots={o:{t for t in structure.inputs[o] if not structure.tensors[t].producers
               and structure.tensors[t].size<=1048576} for o in order}
    degree={o:len(ir.predecessors[o]) for o in order}
    ready=[(rank[o],o) for o in order if not degree[o]];heapq.heapify(ready)
    previous=set();result=[]
    while ready:
        choices=[heapq.heappop(ready) for _ in range(min(window,len(ready)))]
        chosen=min(choices,key=lambda x:(-sum(structure.tensors[t].size for t in roots[x[1]]&previous),x[0]))
        for x in choices:
            if x!=chosen:heapq.heappush(ready,x)
        o=chosen[1];result.append(o);previous=roots[o]
        for c in ir.successors[o]:
            degree[c]-=1
            if not degree[c]:heapq.heappush(ready,(rank[c],c))
    return result


def candidates(ir,problem,cores,parent=None,deadline=float('inf')):
    if problem not in (2,3) or parent is None:raise ValueError('requires P2/P3 parent')
    structure=Structure(ir);owners=owner_map(parent)
    _,order,info=insertion_schedule(structure,cores,fixed=owners,deadline=deadline)
    yield dict(name='residency_gap_control',plan=operation_plan(ir,order,owners,cores),
               metadata=dict(family='gap_control',**info))
    for window in (8,32,128):
        if time.monotonic()>=deadline:raise TimeoutError('residency deadline')
        changed,peak=lifetime_order(ir,owners,order,window,1.)
        yield dict(name=f'gap_lifetime_{window}',plan=operation_plan(ir,changed,owners,cores),
                   metadata=dict(family='gap_lifetime',window=window,surrogate_peak=peak))
    # Joint ownership changes get a newly constructed pipe calendar. Reusing
    # the old pipe order after migration would miss the released parallelism.
    for i,proposal in enumerate(reuse_proposals(ir,owners,{},cores)[:3]):
        if time.monotonic()>=deadline:raise TimeoutError('reuse placement deadline')
        changed=dict(owners);changed.update(proposal['changes'])
        _,new_order,info=insertion_schedule(structure,cores,fixed=changed,deadline=deadline)
        yield dict(name=f'input_region_gap_{i}',plan=operation_plan(ir,new_order,changed,cores),
                   metadata=dict(family='input_region_gap',proposal=proposal,**info))
    if problem==3:
        for window in (8,32):
            changed=input_window(structure,order,window)
            yield dict(name=f'gap_shared_read_{window}',plan=operation_plan(ir,changed,owners,cores),
                metadata=dict(family='gap_shared_read',window=window,
                    scope='bounded ready-order adjacency; official FIFO decides actual hits'))
