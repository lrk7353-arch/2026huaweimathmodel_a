"""Read unchanged compiler contracts and join them to the official global trace.

Observed critical chains and congestion are diagnostic, not counterfactual delay
certificates: DDR uses dynamic bandwidth sharing. Compilation cost is charged
to candidate generation by the caller (which must impose a process deadline).
"""
from collections import Counter, defaultdict
import sys
import time

from common_run import DATA, validate_plan


def pool_intervals(entries):
    events = defaultdict(list)
    for key, op in entries.items():
        events[op['start']].append((1, key))
        events[op['end']].append((-1, key))
    active = set(); previous = 0; busy = overlap = 0; peak = 0
    congestion = Counter()
    for now, changes in sorted(events.items()):
        duration = now-previous
        if active:
            busy += duration
            if len(active)>1:
                overlap += duration
                for key in active: congestion[key] += duration*(len(active)-1)/len(active)
        for delta,key in sorted(changes):
            if delta<0: active.remove(key)
            else: active.add(key)
        peak=max(peak,len(active));previous=now
    return dict(busy_cycles=busy,overlap_cycles=overlap,peak_concurrent=peak),dict(congestion)


def observe(ir, plan, raw, problem):
    started=time.monotonic();validate_plan(ir,plan)
    sys.path.insert(0,str(DATA.parent/'code'))
    from schedule_step3 import _uses_ddr_bandwidth, _op_duration
    if problem==1:
        from multicore_cut_evaluate_problem_1 import _build_scene_a_tasks
        tasks,_,traffic,_=_build_scene_a_tasks(ir.graph,plan,raw['bandwidth_bytes_per_cycle'],raw['capacity_bytes'])
        cross=[]
    else:
        if problem==3:
            from multicore_cut_evaluate_problem_3 import _build_scene_b_tasks
        else:
            from multicore_cut_evaluate_problem_2 import _build_scene_b_tasks
        tasks,cross,_,traffic,_=_build_scene_b_tasks(ir.graph,plan,raw['bandwidth_bytes_per_cycle'],raw['capacity_bytes'])
    assert traffic==raw['data_movement_bytes'],'compiled COPY accounting differs from observation'
    events={};compute={};assignment={};pred=defaultdict(dict);successors=defaultdict(dict)
    for core in raw['per_core_timeline']:
        for entry in core['ops']:
            key=(entry['task_id'],entry['op_id'])
            assert key not in events
            events[key]=dict(entry,core_id=core['core_id'])
            if entry['op_id'] in ir.compute_ids:
                compute[entry['op_id']]=key;assignment[entry['op_id']]=core['core_id']
    assert set(compute)==set(ir.compute_ids)
    def edge(a,b,delay,kind):
        assert events[a]['end']+delay<=events[b]['start'],(a,b,kind,'trace dependency mismatch')
        old=pred[b].get(a)
        if old is None or delay>old[0]: pred[b][a]=(delay,kind);successors[a][b]=delay
    memory_edges=[];copies={};logical_copy=0;path_bytes=Counter();work=Counter();residency=defaultdict(list)
    for task_id,task in tasks.items():
        mem={(e['source'],e['target']):e for e in task['step3']['memory_dependencies']}
        for op,ps in task['op_preds'].items():
            for p in ps:
                kind='memory' if (p,op) in mem else 'data'
                edge((task_id,p),(task_id,op),0,kind)
                if kind=='memory':memory_edges.append(dict(task=task_id,source=p,target=op,**{k:v for k,v in mem[p,op].items() if k not in ('source','target')}))
        for seq in task['pipe_ops'].values():
            for a,b in zip(seq,seq[1:]):edge((task_id,a),(task_id,b),0,'pipe')
        touched=defaultdict(set)
        for oid,op in task['op_by_id'].items():
            key=(task_id,oid);event=events[key]
            for t in task['in_tids'][oid]+task['out_tids'][oid]:touched[t].add(key)
            if op['op'] not in ('COPY_IN','COPY_OUT'):continue
            tids=task['out_tids'][oid] if op['op']=='COPY_IN' else task['in_tids'][oid]
            size=sum(task['tensor_by_id'][t]['size'] for t in tids)
            logical_copy+=size
            ddr=_uses_ddr_bandwidth(op,task['in_tids'],task['out_tids'],task['tensor_by_id'])
            path=event.get('memory_path','DDR' if ddr else 'ON_CHIP')
            assert (path in ('DDR','CACHE_READ'))==ddr
            path_bytes[path]+=size
            nominal=_op_duration(op,task['in_tids'],task['out_tids'],task['tensor_by_id'],
                                 raw.get('cache_bandwidth_bytes_per_cycle',250) if path=='CACHE_READ' else raw['bandwidth_bytes_per_cycle'])
            work[path]+=nominal
            copies[key]=dict(event,size_bytes=size,path=path,nominal_cycles=nominal,
                duration_excess=event['duration']-nominal,
                original_tensors=sorted(t for t in tids if t in ir.tensors))
        for t,ops in touched.items():
            tensor=task['tensor_by_id'][t]
            if tensor['pos']=='DDR':continue
            first=min(events[o]['start'] for o in ops);last=max(events[o]['end'] for o in ops)
            residency[task['core_id'],tensor['pos']].extend(((first,tensor['size']),(last,-tensor['size'])))
    assert logical_copy==traffic['scheduled_copy_bytes']
    assert sum(path_bytes.values())==logical_copy
    if problem==3:assert path_bytes['CACHE_READ']==raw['cache_stats']['hit_bytes']
    for link in cross:
        edge((link['source_core'],link['source_copy_out_id']),
             (link['target_core'],link['target_copy_in_id']),raw['cross_core_copy_delay_cycles'],'cross_core')
    if problem==1:
        first={t:min(((t,o) for o in task['seq']),key=lambda k:events[k]['start']) for t,task in tasks.items()}
        last={t:max(((t,o) for o in task['seq']),key=lambda k:events[k]['end']) for t,task in tasks.items()}
        for t,task in tasks.items():
            for p in task['pred_tasks']:
                delay=raw['task_cross_core_wait_cycles'] if tasks[p]['core_id']!=task['core_id'] else 0
                edge(last[p],first[t],delay,'task_data')
        for seq in plan['core_schedules']:
            for a,b in zip(seq,seq[1:]):edge(last[a],first[b],raw['task_same_core_wait_cycles'],'task_core')
    ordered=sorted(events,key=lambda k:(events[k]['start'],events[k]['end'],k))
    tails={}
    for key in reversed(ordered):
        tails[key]=events[key]['duration']+max((d+tails[n] for n,d in successors[key].items()),default=0)
    span=raw['makespan'];slack={k:max(0,span-events[k]['start']-tails[k]) for k in events}
    chain=[];key=max(events,key=lambda k:events[k]['end']) if events else None
    while key is not None:
        chain.append(key)
        key=max(pred[key],key=lambda p:(events[p]['end']+pred[key][p][0],p),default=None)
    pools={};congestion={}
    for path in ('DDR','CACHE_READ'):
        pools[path],cost=pool_intervals({k:e for k,e in copies.items() if e['path']==path});congestion.update(cost)
    for k,e in copies.items():
        e['slack']=slack[k];e['congestion_exposure']=congestion.get(k,0)
        e['wait_priority']=(e['nominal_cycles']+e['duration_excess']+e['congestion_exposure'])/(1+slack[k]/max(1,.03*span))
    peaks={};areas={}
    for key,points in residency.items():
        level=peak=area=0;previous=0
        for now,delta in sorted(points):
            area+=(now-previous)*level;level+=delta;peak=max(peak,level);previous=now
        assert level==0
        peaks[f'{key[0]}:{key[1]}']=peak;areas[f'{key[0]}:{key[1]}']=area
    summary=dict(makespan=span,official_copy=traffic,path_bytes=dict(path_bytes),
        nominal_pool_work_cycles=dict(work),pool_occupancy=pools,
        projected_live_peak=peaks,projected_live_byte_cycles=areas,
        memory_dependency_count=len(memory_edges),critical_chain_ops=len(chain),
        critical_chain_memory_edges=sum(pred[b].get(a,(0,''))[1]=='memory' for a,b in zip(chain[1:],chain)),
        top_copy_waits=[dict(copies[k],task=k[0]) for k in sorted(copies,key=lambda k:-copies[k]['wait_priority'])[:20]],
        compile_seconds=time.monotonic()-started,
        limitations='Observed durations include dynamic contention; chain/slack and projected residency are ranking diagnostics, not removable-delay certificates.')
    return dict(summary=summary,events=events,compute=compute,assignment=assignment,pred=pred,
                successors=successors,tails=tails,slack=slack,chain=chain,copies=copies,memory_edges=memory_edges)
