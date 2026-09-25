"""Cross-evaluate identical plans in P2/P3; separate scheduling and cache effects."""
import argparse,time
from concurrent.futures import ProcessPoolExecutor,as_completed
from common_run import *
from advanced_solver.cache_refine import _core_map


def one(case,source,incumbent,out):
    out=Path(out)/case;out.mkdir(parents=True,exist_ok=False)
    s=read_json(Path(source)/case/'summary.json')
    controls=[t for t in s['calls'] if t['metadata']['is_reencoding_control'] and t['record']['status']=='success']
    guided=[t for t in s['calls'] if not t['metadata']['is_reencoding_control'] and t['record']['status']=='success']
    if not controls:raise ValueError('missing successful encoding control')
    control=controls[0];selected=min(guided,key=lambda t:score(t['record'])) if guided else None
    plans={'input':incumbent,'control':control['record']}
    if selected:plans['guided']=selected['record']
    old_plan=read_json(incumbent['plan_path']);control_plan=read_json(control['record']['plan_path'])
    assert _core_map(old_plan)==_core_map(control_plan)
    if selected:
        chosen_plan=read_json(selected['record']['plan_path'])
        assert chosen_plan['node_to_subgraph']==control_plan['node_to_subgraph']
        assert _core_map(chosen_plan)==_core_map(control_plan)
    records={};calls=[]
    for name,record in plans.items():
        plan=read_json(record['plan_path']);records[name]={}
        atomic_json(out/(name+'.plan.json'),plan)
        for problem in (2,3):
            r=evaluate(DATA/(case+'.json'),plan,problem,R/'advanced_solver/runs/formal_v2/evaluations',timeout=60,config_path=DATA/'config.txt')
            calls.append(r);records[name][str(problem)]=r
    row=dict(case=case,has_guided=selected is not None,guided_name=selected['name'] if selected else None,
             logical_calls=len(calls),new_calls=sum(not r['cache_hit'] for r in calls),
             all_success=all(r['status']=='success' for r in calls))
    for name,pair in records.items():
        for problem,r in pair.items():
            row[f'{name}_p{problem}']=score(r)[0] if r['status']=='success' else None
            row[f'{name}_p{problem}_copy']=score(r)[1] if r['status']=='success' else None
        r=pair['3'];row[name+'_hit_rate']=r['metrics']['cache_stats']['hit_rate'] if r['status']=='success' else None
    if row['all_success'] and selected:
        row['p3_schedule_gain']=row['control_p3']-row['guided_p3']
        row['p2_schedule_gain']=row['control_p2']-row['guided_p2']
        row['control_cache_gain']=row['control_p2']-row['control_p3']
        row['guided_cache_gain']=row['guided_p2']-row['guided_p3']
        row['cache_interaction']=row['p3_schedule_gain']-row['p2_schedule_gain']
        assert row['cache_interaction']==row['guided_cache_gain']-row['control_cache_gain']
        row['guided_improves_incumbent']=row['guided_p3']<row['input_p3']
    atomic_json(out/'summary.json',dict(row=row,records=records,selected_metadata=selected['metadata'] if selected else None,
        scope='P3-selected candidate; per-plan counterfactual comparison, not an unbiased independent algorithm ablation; spill may change with schedules'))
    return row


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--before',required=True);p.add_argument('--out',required=True);a=p.parse_args()
    out=Path(a.out);out.mkdir(parents=True,exist_ok=False);start=time.monotonic();rows=[]
    s=read_json(Path(a.source)/'summary.json');assert s['complete'];cases=[r['case'] for r in s['rows']]
    before={key(r):r for r in read_json(a.before)['records']}
    with ProcessPoolExecutor(max_workers=2) as pool:
        fs=[pool.submit(one,c,a.source,before[c,3,5],out) for c in cases]
        for f in as_completed(fs):
            row=f.result();rows.append(row);write_csv(out/'results.csv',rows)
            atomic_json(out/'progress.json',dict(completed=len(rows),expected=len(cases),rows=rows));print(json.dumps(row),flush=True)
    atomic_json(out/'summary.json',dict(complete=True,completed=len(rows),expected=len(cases),rows=rows,wall_seconds=time.monotonic()-start))
