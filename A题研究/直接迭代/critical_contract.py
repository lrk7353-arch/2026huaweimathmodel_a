"""Contract observed P1 critical-chain boundaries, including cross-core ones.

The input trace has already been charged. No official graph recompilation is
needed for candidate generation. Quotient SCC closure makes every contraction
legal, including bypass paths outside the chosen critical-chain segment.
"""
from collections import defaultdict
import time

from barrier_bands import contract_cycles
from p1_selective import block_views, _assign
from unified_structure import Structure


def chain(raw):
    tasks={};owners={};previous={};parents=defaultdict(list)
    for core in raw['per_core_timeline']:
        for i,t in enumerate(core['tasks']):
            task=t['task_id'];tasks[task]=t;owners[task]=core['core_id']
            if i:previous[task]=core['tasks'][i-1]['task_id']
    for edge in raw['task_dependencies']:parents[edge['target']].append(edge['source'])
    current=max(tasks,key=lambda x:tasks[x]['end']);path=[]
    while True:
        choices=[(tasks[p]['end']+(1000 if owners[p]!=owners[current] else 0),p) for p in parents[current]]
        if current in previous:choices.append((tasks[previous[current]]['end']+100,previous[current]))
        if not choices:path.append((current,0));break
        _,parent=max(choices);path.append((current,tasks[current]['start']-tasks[parent]['end']));current=parent
    return list(reversed(path)),tasks


def candidates(ir,problem,cores,parent,raw,deadline=float('inf')):
    if problem!=1:raise ValueError('critical contraction requires P1')
    structure=Structure(ir);path,tasks=chain(raw)
    original=defaultdict(list)
    for o in structure.topo:original[parent['node_to_subgraph'][str(o)]].append(o)
    proposals=[]
    for length in (2,4,8):
        ranked=[]
        for start in range(len(path)-length+1):
            segment=path[start:start+length];saved=sum(w for _,w in segment[1:])
            duration=sum(tasks[t]['duration'] for t,_ in segment)
            ranked.append((saved/max(1,duration),saved,tuple(t for t,_ in segment)))
        for _,saved,members in sorted(ranked,reverse=True)[:3]:
            if saved>0:proposals.append((members,saved))
    # Interleave small and broad contractions by their observed removable
    # boundary time. It is not a promised makespan saving after reassignment.
    proposals.sort(key=lambda x:-x[1])
    for i,(members,saved) in enumerate(proposals):
        if time.monotonic()>=deadline:raise TimeoutError('critical contraction deadline')
        chosen=set(members);groups=[b for t,b in original.items() if t not in chosen]
        groups.append([o for t in members for o in original[t]])
        blocks=contract_cycles(ir,groups,structure.topo)
        plan,proxy=_assign(ir,blocks,block_views(ir,blocks),cores,cores,'eft')
        yield dict(name=f'critical_contract_{i}',plan=plan,metadata=dict(family='critical_contract',
            selected_tasks=members,observed_boundary_cycles=saved,before_tasks=len(original),
            after_tasks=len(blocks),task_proxy=proxy,scope='complete merge, SCC closure and global Task placement'))
