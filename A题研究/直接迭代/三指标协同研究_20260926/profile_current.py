"""Profile all 100 current five-core plans using existing ledgers and static models."""
import csv,json,hashlib,sys
from collections import Counter,defaultdict
from pathlib import Path
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parent))
from common_run import DATA,GraphIR,read_json,write_csv
from advanced_solver.trace_refine import _tensor_views,_plan_assignment
from p23_data_refine import partition_copy_bytes
from p1_boundary_lower_bound import boundary_ddr_lower_bound
from p1_selective import topological_order
FINAL=HERE.parent/'联合整合_20260926/最终累计'
def main():
    ledger=list(csv.DictReader((FINAL/'累计1500配置成绩.csv').open(encoding='utf-8-sig')))
    selected=[r for r in ledger if r['cores']=='5'];rows=[];known={}
    base=HERE.parent/'联合整合_20260926'
    for item in read_json(base/'累计并集/复评记录.json')['records']:
        rec=item['record'];known[item['case'],item['problem'],item['cores']]=rec
    for item in read_json(base/'新增精选复评/结果.json')['records']:
        if item['replay_matches']:known[item['case'],item['problem'],item['cores']]=item['replay_record']
    model_checked=0;cache_records=0
    for i in range(1,101):
        case=f'case_{i:03d}';ir=GraphIR.from_path(DATA/(case+'.json'));views=_tensor_views(ir)
        order=topological_order(ir,'stable_id');ends={}
        for o in order:ends[o]=max(1,ir.ops[o]['cycles'])+max((ends[v] for v in ir.predecessors[o]),default=0)
        # Original volume, using the exact official COPY endpoint convention.
        tin,tout=defaultdict(list),defaultdict(list)
        for e in ir.graph['edges']:
            if e['source'] in ir.ops:tout[e['source']].append(e['target'])
            elif e['target'] in ir.ops:tin[e['target']].append(e['source'])
        original=sum(sum(ir.tensors[t]['size'] for t in (tout[o] if op['op']=='COPY_IN' else tin[o]))
                     for o,op in ir.ops.items() if op['op'] in ('COPY_IN','COPY_OUT'))
        for record in (r for r in selected if r['case']==case):
            p=int(record['problem']);plan=read_json(FINAL/record['plan']);span=int(record['makespan'])
            canonical=json.dumps(plan,ensure_ascii=False,separators=(',',':')).encode()
            assert hashlib.sha256(canonical).hexdigest()==record['plan_sha256'],(case,p,'plan_hash_mismatch')
            owner=_plan_assignment(ir,plan,5);mapping={int(k):v for k,v in plan['node_to_subgraph'].items()}
            work=Counter();taskwork=Counter()
            for op in ir.compute_ids:
                pipe=ir.ops[op]['pipe'];cycles=max(1,ir.ops[op]['cycles'])
                if pipe in ('PIPE_M','PIPE_V'):
                    work[owner[op],pipe]+=cycles;taskwork[mapping[op],pipe]+=cycles
            if p==1:pre=boundary_ddr_lower_bound(ir,plan)['mandatory_boundary']['total_bytes']
            else:pre=partition_copy_bytes(ir,owner,views)
            total=original+int(record['added_copy']);spill=total-pre
            assert spill>=0,(case,p,spill)
            replica=0;root=0
            for tid,cons in views[1].items():
                if views[0][tid] or not cons:continue
                destinations={mapping[o] if p==1 else owner[o] for o in cons}
                root+=len(destinations)*ir.tensors[tid]['size']
                replica+=max(0,len(destinations)-1)*ir.tensors[tid]['size']
            actual=known.get((case,p,5));verified=False
            if actual:
                # Frozen records embed hashes, so replay directories are not needed.
                hashes=actual['hashes']
                if (hashes['plan_sha256']!=record['plan_sha256'] or
                    hashes['graph_sha256']!=hashlib.sha256((DATA/(case+'.json')).read_bytes()).hexdigest()):actual=None
            if actual and (actual['metrics']['makespan'],actual['metrics']['data_movement_bytes']['added_copy_bytes'])==(span,int(record['added_copy'])):
                m=actual['metrics']['data_movement_bytes']
                assert m['original_graph_copy_bytes']==original and m['scheduled_copy_bytes']==total
                assert m['spill_added_copy_bytes']==spill,(case,p,'spill_model_mismatch')
                verified=True;model_checked+=1
            else:actual=None
            cache=actual['metrics'].get('cache_stats',{}) if actual else {}
            if cache:cache_records+=1
            rows.append(dict(case=case,problem=p,cores=5,makespan=span,speedup=float(record['speedup']),
                original_bytes=original,added_bytes=int(record['added_copy']),scheduled_bytes=total,
                modeled_pre_spill_bytes=pre,inferred_spill_bytes=spill,spill_model_checked_against_record=verified,
                root_read_bytes=root,root_replication_bytes=replica,
                root_replication_fraction=replica/max(1,pre),
                active_cores=len(set(owner.values())),subgraphs=len(set(mapping.values())),
                fixed_core_compute_fraction=max(work.values(),default=0)/span,
                global_compute_floor_fraction=max(ir.total_work_m,ir.total_work_v)/5/span,
                heaviest_subgraph_compute_fraction=max(taskwork.values(),default=0)/span,
                original_compute_path_fraction=max(ends.values(),default=0)/span,
                logical_ddr_volume_over_time=total/60/span,
                hit_rate_if_available=cache.get('hit_rate'),cache_record_available=bool(cache),
                plan_sha256=record['plan_sha256']))
    write_csv(HERE/'当前100图五核剖面.csv',rows)
    summary=[]
    for p in (1,2,3):
        rs=[r for r in rows if r['problem']==p]
        summary.append(dict(problem=p,graphs=len(rs),mean_speedup=sum(r['speedup'] for r in rs)/len(rs),
            inferred_spill_graphs=sum(r['inferred_spill_bytes']>0 for r in rs),
            root_replication_over25pct=sum(r['root_replication_fraction']>=.25 for r in rs),
            fixed_core_compute_over80pct=sum(r['fixed_core_compute_fraction']>=.8 for r in rs),
            global_compute_floor_over90pct=sum(r['global_compute_floor_fraction']>=.9 for r in rs),
            heaviest_subgraph_compute_over40pct=sum(r['heaviest_subgraph_compute_fraction']>=.4 for r in rs),
            logical_volume_over60pct=sum(r['logical_ddr_volume_over_time']>=.6 for r in rs) if p!=3 else None,
            actual_cache_records=sum(r['cache_record_available'] for r in rs)))
    result=dict(official_calls=0,graphs=100,plans=300,model_checked_current_records=model_checked,
        actual_current_p3_cache_records=cache_records,summary=summary,
        scope='Static structural volume from official-compatible models; spill inferred from saved total volume. Cache missing is unknown, never zero. Thresholds descriptive and not validated routing rules.',
        ledger_sha256=hashlib.sha256((FINAL/'累计1500配置成绩.csv').read_bytes()).hexdigest())
    (HERE/'当前图池摘要.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
