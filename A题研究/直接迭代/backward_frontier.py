"""Build a reverse-time proxy, then return plans for the untouched original DAG.

Forward Task sealing protects release of fork outputs. Reverse sealing instead
keeps a join's producer region together until reverse dependencies force cuts.
Only candidate construction uses the reversed copy; official input is unchanged.
"""
from common_run import GraphIR, validate_plan
from event_frontier import insertion_schedule, operation_plan, sealed_tasks
from unified_structure import Structure


def reverse_view(ir):
    reverse_pipe={'PIPE_MTE2':'PIPE_MTE3','PIPE_MTE3':'PIPE_MTE2'}
    reverse_copy={'COPY_IN':'COPY_OUT','COPY_OUT':'COPY_IN'}
    graph=dict(ir.graph)
    graph['ops']=[dict(o,op=reverse_copy.get(o['op'],o['op']),pipe=reverse_pipe.get(o['pipe'],o['pipe']))
                  for o in ir.graph['ops']]
    graph['tensors']=[dict(t) for t in ir.graph['tensors']]
    graph['edges']=[dict(e,source=e['target'],target=e['source']) for e in ir.graph['edges']]
    return GraphIR.from_graph(graph)


def candidates(ir,problem,cores,parent=None,deadline=float('inf')):
    reverse=reverse_view(ir);structure=Structure(reverse)
    fixed=None
    specs=[('critical',.25,0.),('critical',1.,0.),('release',1.,.25),
           ('stable',1.,0.),('critical',2.,.25),('release',.25,0.)]
    if parent:
        by_task={t:c for c,seq in enumerate(parent['core_schedules']) for t in seq}
        fixed={int(o):by_task[t] for o,t in parent['node_to_subgraph'].items()}
        specs=[('parent',1.,0.)]+specs
    for i,(ordering,weight,locality) in enumerate(specs):
        owners,reverse_order,info=insertion_schedule(structure,cores,
            'critical' if ordering=='parent' else ordering,weight,locality,
            fixed if ordering=='parent' else None,deadline)
        order=list(reversed(reverse_order))
        meta=dict(family='backward_frontier',ordering=ordering,communication=weight,locality=locality,
                  fixed_assignment=ordering=='parent',proxy_direction='reversed graph for proposals only',**info)
        if problem in (2,3):
            yield dict(name=f'backward_{i}_{ordering}',plan=operation_plan(ir,order,owners,cores),metadata=meta)
            continue
        cap=max(1000,max(ir.total_work_m,ir.total_work_v)/max(1,cores*4))
        for bound in (float('inf'),cap):
            revplan,blocks=sealed_tasks(reverse,reverse_order,owners,cores,bound)
            # Reverse both task precedence and the sequential order on each core.
            plan=dict(node_to_subgraph={str(o):revplan['node_to_subgraph'][str(o)] for o in order},
                      core_schedules=[list(reversed(seq)) for seq in revplan['core_schedules']])
            validate_plan(ir,plan)
            label='open' if bound==float('inf') else 'capped'
            yield dict(name=f'backward_{i}_{ordering}_{label}',plan=plan,
                       metadata=dict(meta,task_count=len(blocks),cap=None if label=='open' else bound))
            from p1_selective import _toposort_blocks,block_views,_assign
            blocks=_toposort_blocks(ir,blocks,order)
            assigned,proxy=_assign(ir,blocks,block_views(ir,blocks),cores,cores,'eft')
            yield dict(name=f'backward_{i}_{ordering}_{label}_reassign',plan=assigned,
                       metadata=dict(meta,task_count=len(blocks),global_reassignment=True,task_proxy=proxy))
