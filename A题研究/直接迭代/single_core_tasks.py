"""Bounded Task size for P1 single-core coverage; final cost must be official-evaluated."""
from common_run import validate_plan

def bounded_plan(ir, max_ops=1024):
    if type(max_ops) is not int or max_ops<1:raise ValueError('max_ops must be positive integer')
    # Independent small WCCs may share a Task. Large WCCs use contiguous chunks
    # of a topological ordering, so their quotient cannot contain cycles.
    from p1_selective import topological_order
    order=topological_order(ir,'stable_id');by_component={c.id:[] for c in ir.components}
    for op in order:by_component[ir.component_by_op[op]].append(op)
    blocks=[];bucket=[]
    for c in ir.components:
        nodes=by_component[c.id]
        if len(nodes)>max_ops:
            if bucket:blocks.append(bucket);bucket=[]
            blocks.extend(nodes[i:i+max_ops] for i in range(0,len(nodes),max_ops))
        else:
            if len(bucket)+len(nodes)>max_ops:blocks.append(bucket);bucket=[]
            bucket.extend(nodes)
    if bucket:blocks.append(bucket)
    plan={'node_to_subgraph':{str(op):sg for sg,nodes in enumerate(blocks) for op in nodes},
          'core_schedules':[list(range(len(blocks)))]}
    validate_plan(ir,plan)
    return plan
