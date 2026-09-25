"""P1 coarse wavefront contraction with explicit quotient-cycle repair.

Original ASAP/ALAP dependency coordinates define monotone bands. Within a
band, owner clusters may form a quotient cycle; every strongly connected
cluster is merged before Task reassignment. This deliberately trades some
fine-grained concurrency for fewer whole-Task barriers and DDR boundaries.
"""
from collections import defaultdict
import time

from event_frontier import insertion_schedule
from p1_selective import _toposort_blocks, block_views, _assign
from unified_structure import Structure, owner_map


def contract_cycles(ir, groups, order):
    blocks = list(groups)
    owner = {o:i for i,b in enumerate(blocks) for o in b}
    edges = [set() for _ in blocks]; reverse = [set() for _ in blocks]
    for o in order:
        for p in ir.predecessors[o]:
            a,b=owner[p],owner[o]
            if a!=b: edges[a].add(b);reverse[b].add(a)
    seen=set();finish=[]
    for root in range(len(blocks)):
        if root in seen:continue
        seen.add(root);stack=[(root,iter(sorted(edges[root])))]
        while stack:
            node,children=stack[-1]
            child=next(children,None)
            if child is None:finish.append(node);stack.pop()
            elif child not in seen:seen.add(child);stack.append((child,iter(sorted(edges[child]))))
    components={};count=0
    for root in reversed(finish):
        if root in components:continue
        components[root]=count;stack=[root]
        while stack:
            for child in reverse[stack.pop()]:
                if child not in components:components[child]=count;stack.append(child)
        count+=1
    merged=defaultdict(list)
    for o in order:merged[components[owner[o]]].append(o)
    return _toposort_blocks(ir,list(merged.values()),order)


def candidates(ir, problem, cores, parent=None, deadline=float('inf')):
    if problem!=1:raise ValueError('barrier bands only model P1 Tasks')
    structure=Structure(ir)
    layouts=[]
    if parent is not None:layouts.append(('parent',owner_map(parent)))
    layout,_,_=insertion_schedule(structure,cores,communication=2.,locality=.25,deadline=deadline)
    layouts.append(('global',layout))
    critical=max(structure.tail.values(),default=1)
    for bands in (4,2,8,16):
        for direction in ('asap','alap'):
            for name,owners in layouts:
                if time.monotonic()>=deadline:raise TimeoutError('barrier band deadline')
                grouped=defaultdict(list)
                for o in structure.topo:
                    coordinate=structure.earliest[o] if direction=='asap' else critical-structure.tail[o]
                    band=min(bands-1,int(coordinate*bands/max(1,critical)))
                    grouped[band,owners[o]].append(o)
                blocks=contract_cycles(ir,list(grouped.values()),structure.topo)
                view=block_views(ir,blocks)
                plan,proxy=_assign(ir,blocks,view,cores,cores,'eft')
                yield dict(name=f'bands_{bands}_{direction}_{name}',plan=plan,
                    metadata=dict(family='barrier_bands',bands=bands,direction=direction,
                        layout=name,initial_groups=len(grouped),tasks=len(blocks),task_proxy=proxy,
                        scope='original dependency coordinates; quotient SCC merging; full Task reassignment'))
