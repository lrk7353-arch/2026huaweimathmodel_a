"""Read-only whole-pool characterization, without new official evaluations.

Labels overlap and are descriptive. Thresholds are exploratory and never select
plans by graph filename. COPY reuse is counted by the official logical tensor,
not by a potentially different root DDR id. Estimates omit spill and FIFO timing.
"""
from collections import Counter, defaultdict
import csv
import hashlib
import math
from pathlib import Path
from common_run import DATA, GraphIR, atomic_json, read_json, write_csv
from p1_selective import topological_order

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
OUT=HERE/'全场景架构研究_20260925'
LEDGER=HERE/'闭环验证_20260925/下一阶段攻坚/重划实证_v1/累计1500配置成绩.csv'


def owner_map(plan):
    owner={sg:c for c,seq in enumerate(plan['core_schedules']) for sg in seq}
    return {int(o):owner[sg] for o,sg in plan['node_to_subgraph'].items()}


def profile(ir, rows):
    compute=set(ir.compute_ids);order=topological_order(ir,'stable_id');end={}
    for o in order:end[o]=max(1,ir.ops[o]['cycles'])+max((end[p] for p in ir.predecessors[o]),default=0)
    total=ir.total_work_m+ir.total_work_v
    comps=[c.compute_work for c in ir.components]
    producers,consumers=defaultdict(set),defaultdict(set)
    for e in ir.graph['edges']:
        a,b=e['source'],e['target']
        if a in compute:producers[b].add(a)
        elif b in compute:consumers[a].add(b)
    base=dict(case=ir.path.stem,compute_ops=len(compute),components=len(comps),
        largest_component_work_fraction=max(comps,default=0)/max(1,total),
        compute_work_m=ir.total_work_m,compute_work_v=ir.total_work_v,
        smaller_pipe_fraction=min(ir.total_work_m,ir.total_work_v)/max(1,total),
        dependency_path_proxy=max(end.values(),default=0),
        average_compute_cycles=total/max(1,len(compute)),
        fork_fraction=sum(len(ir.successors[o])>1 for o in compute)/max(1,len(compute)),
        join_fraction=sum(len(ir.predecessors[o])>1 for o in compute)/max(1,len(compute)),
        graph_sha256=hashlib.sha256(ir.path.read_bytes()).hexdigest())
    base['parallelism_proxy']=total/max(1,base['dependency_path_proxy'])
    for problem in (1,2,3):
        row=rows[problem];plan=read_json(ROOT/row['plan']);owner=owner_map(plan)
        core_m=[0]*5;core_v=[0]*5;tasks=defaultdict(lambda:[0,0]);taskcounts=Counter()
        for o in compute:
            idx=0 if ir.ops[o]['pipe']=='PIPE_M' else 1
            (core_m if idx==0 else core_v)[owner[o]]+=max(1,ir.ops[o]['cycles'])
            sg=plan['node_to_subgraph'][str(o)];tasks[sg][idx]+=max(1,ir.ops[o]['cycles']);taskcounts[sg]+=1
        t=int(row['makespan']);read_bytes=repeat_bytes=0;shared_keys=0
        for tid,tensor in ir.tensors.items():
            src={owner[o] for o in producers[tid]};dst={owner[o] for o in consumers[tid]}
            reads=len(dst) if not src else sum(a!=b for a in src for b in dst)
            read_bytes+=reads*tensor['size']
            if reads>1 and tensor['size']<=1048576:
                repeat_bytes+=(reads-1)*tensor['size'];shared_keys+=1
        # Safe compute floor, independent of partition. Its complement describes
        # an upper bound on possible time reduction, not an attainable target.
        floor=math.ceil(max(ir.total_work_m,ir.total_work_v)/5)
        values=dict(makespan=t,speedup=float(row['speedup']),added_copy=int(row['added_copy']),
            active_cores=len(set(owner.values())),subgraphs=len(tasks),
            tiny_subgraph_fraction=sum(x<=8 for x in taskcounts.values())/max(1,len(tasks)),
            largest_subgraph_work_fraction=max((max(w) for w in tasks.values()),default=0)/max(1,t),
            fixed_core_work_fraction=max(core_m+core_v)/max(1,t),
            global_compute_floor_fraction=floor/max(1,t),
            modeled_copy_in_bytes=read_bytes,modeled_repeat_cacheable_bytes=repeat_bytes,
            modeled_repeat_fraction=repeat_bytes/max(1,read_bytes),shared_logical_keys=shared_keys)
        base.update({f'p{problem}_{k}':v for k,v in values.items()})
    labels=[]
    if len(comps)>=5 and base['largest_component_work_fraction']<=.35:labels.append('many_independent_components')
    if base['largest_component_work_fraction']>=.6:labels.append('dominant_connected_region')
    if base['parallelism_proxy']>=10 and base['fork_fraction']>=.05:labels.append('fork_join_parallel_potential')
    if base['p1_largest_subgraph_work_fraction']>=.4:labels.append('p1_heavy_task')
    if base['average_compute_cycles']<=100:labels.append('fine_grained_compute')
    if base['p3_modeled_repeat_fraction']>=.25:labels.append('repeated_logical_reads')
    if base['smaller_pipe_fraction']>=.2:labels.append('mixed_m_v_work')
    base['overlapping_structural_labels']=';'.join(labels)
    return base


def main():
    ledger=list(csv.DictReader(LEDGER.open(encoding='utf-8-sig')))
    lookup={(x['case'],int(x['problem'])):x for x in ledger if x['cores']=='5'}
    rows=[profile(GraphIR.from_path(path),{p:lookup[path.stem,p] for p in (1,2,3)}) for path in sorted(DATA.glob('case_*.json'))]
    write_csv(OUT/'100图结构与五核现状.csv',rows)
    summary={'graph_count':len(rows),'official_evaluations':0,
        'source_ledger':str(LEDGER.relative_to(ROOT)),'ledger_sha256':hashlib.sha256(LEDGER.read_bytes()).hexdigest(),
        'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'thresholds_scope':'Exploratory overlapping descriptive labels; not validated routing rules.',
        'compute_floor_scope':'max(total PIPE_M work, total PIPE_V work)/5 is necessary, not achievable.',
        'cache_scope':'Reconstructed logical COPY_IN reuse potential; ignores timing, eviction, spills and criticality. Not actual hits.',
        'label_counts':dict(Counter(label for row in rows for label in row['overlapping_structural_labels'].split(';') if label)),
        'scene_summary':{str(p):{'mean_speedup':sum(x[f'p{p}_speedup'] for x in rows)/len(rows),
            'below2':sum(x[f'p{p}_speedup']<2 for x in rows),
            'compute_floor_at_least90pct':sum(x[f'p{p}_global_compute_floor_fraction']>=.9 for x in rows),
            'compute_floor_at_least80pct':sum(x[f'p{p}_global_compute_floor_fraction']>=.8 for x in rows)} for p in (1,2,3)}}
    atomic_json(OUT/'图池概览.json',summary);print(summary)


if __name__=='__main__':main()
