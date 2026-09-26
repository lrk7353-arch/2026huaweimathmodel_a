"""Read-only P1 audit of the portable release; no official evaluations."""
import csv
import heapq
import json
from pathlib import Path
import tarfile

from common_run import DATA, GraphIR, write_csv
from p1_task_refine import view
from p1_selective import task_lower_bound
from p1_boundary_lower_bound import boundary_ddr_lower_bound

HERE = Path(__file__).parent
RELEASE = HERE / '真机启发攻坚_20260926'
OUT = HERE / 'P1最新版本审阅_20260926'


def rows(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def plans(path):
    result = {}
    with tarfile.open(path, 'r:gz') as archive:
        for item in archive:
            if item.isfile() and '/p1/n5/' in item.name and item.name.endswith('.json'):
                case = Path(item.name).name[:8]
                result[case] = json.load(archive.extractfile(item))
    assert len(result) == 100
    return result


def describe(ir, plan, makespan):
    fixed = task_lower_bound(ir, plan)
    _, owner, nodes, edges, preds = view(ir, plan)
    indegree = {t: len(preds[t]) for t in nodes}
    ready = [t for t in nodes if not indegree[t]]
    heapq.heapify(ready)
    end = {}
    while ready:
        t = heapq.heappop(ready)
        end[t] = fixed['task_duration_bounds'][t] + max((end[p] for p in preds[t]), default=0)
        for child in edges[t]:
            indegree[child] -= 1
            if not indegree[child]:
                heapq.heappush(ready, child)
    assert len(end) == len(nodes)
    ddr = boundary_ddr_lower_bound(ir, plan)
    pipe_work = [dict() for _ in plan['core_schedules']]
    for task, ops in nodes.items():
        w = pipe_work[owner[task]]
        for op in ops:
            pipe = ir.ops[op]['pipe']
            w[pipe] = w.get(pipe, 0) + max(1, ir.ops[op]['cycles'])
    return dict(tasks=len(nodes), max_task_ops=max(map(len, nodes.values())),
                partition_path_lb=max(end.values()), fixed_plan_lb=fixed['value'],
                busiest_core_compute_lb=max(max(w.values(), default=0) for w in pipe_work),
                boundary_ddr_lb=ddr['lower_bound'], boundary_bytes=ddr['mandatory_boundary']['total_bytes'],
                fixed_lb_fraction=max(fixed['value'], ddr['lower_bound'])/makespan)


def main():
    selected = {(r['case'], r['cores']): r for r in rows(RELEASE/'精选完整1500/累计1500配置成绩.csv') if r['problem']=='1'}
    cold = {(r['case'], r['cores']): r for r in rows(RELEASE/'从头完整1500/从头1500配置成绩.csv') if r['problem']=='1'}
    chosen_plans = plans(RELEASE/'精选完整1500/selected_plans.tar.gz')
    cold_plans = plans(RELEASE/'从头完整1500/plans.tar.gz')
    evidence = []
    for case in sorted(chosen_plans):
        s, c = selected[case, '5'], cold[case, '5']
        ir = GraphIR.from_path(DATA/(case+'.json'))
        row = dict(case=case, ops=len(ir.compute_ids), wcc=len(ir.components),
                   max_wcc_ops=max(len(x.nodes) for x in ir.components),
                   singlecore=int(s['original_singlecore']), selected_cycles=int(s['makespan']),
                   cold_cycles=int(c['makespan']), selected_speedup=float(s['speedup']),
                   cold_speedup=float(c['speedup']),
                   mean_gap_contribution=(float(s['speedup'])-float(c['speedup']))/100,
                   cold_calls=int(c['calls']), cold_failed=int(c['failed_calls']), cold_seconds=float(c['seconds']))
        for kind, plan, cycles in [('selected',chosen_plans[case],row['selected_cycles']), ('cold',cold_plans[case],row['cold_cycles'])]:
            row.update({kind+'_'+k: v for k,v in describe(ir,plan,cycles).items()})
        evidence.append(row)
    evidence.sort(key=lambda r:-r['mean_gap_contribution'])
    write_csv(OUT/'五核100图诊断.csv',evidence)
    summary = []
    for core in map(str,range(1,6)):
        ss=[selected[k] for k in selected if k[1]==core]
        cc=[cold[k] for k in cold if k[1]==core]
        summary.append(dict(cores=core, selected_mean=sum(float(x['speedup']) for x in ss)/100 if core!='1' else 1,
                            cold_mean=sum(float(x['speedup']) for x in cc)/100 if core!='1' else 1,
                            same_time=sum(selected[k]['makespan']==cold[k]['makespan'] for k in selected if k[1]==core)))
    write_csv(OUT/'核数均值复算.csv',summary)
    print(json.dumps(dict(summary=summary, gap=sum(r['mean_gap_contribution'] for r in evidence),
        top8_fraction=sum(r['mean_gap_contribution'] for r in evidence[:8])/sum(r['mean_gap_contribution'] for r in evidence),
        top10=[{k:r[k] for k in ('case','cold_cycles','selected_cycles','mean_gap_contribution','cold_partition_path_lb','selected_partition_path_lb','cold_calls','cold_seconds')} for r in evidence[:10]]),ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
