"""Collect completed round-13 comparisons, provenance audits and export evidence."""
import shutil
from common_run import *
from analyze_cold_panel import analyze
from analyze_search_budget import analyze as audit_parents


def collect():
    out=Path('第十三轮成果');runs=Path('运行结果')
    panels={'P1扩展':'端到端第十三轮P1扩展_v1','P23开发v1':'端到端第十三轮P23开发_v1',
            'P23开发v2':'端到端第十三轮P23开发_v2','P23验证v2':'端到端第十三轮P23验证_v2'}
    costs=[];comparisons=[];common=[];effects=[]
    for label,name in panels.items():
        root=runs/name;a=analyze(root);e=audit_parents(root);s=read_json(root/'summary.json')
        rows=s['rows']
        costs.append(dict(panel=label,runs=len(rows),successful_runs=sum(r['status']=='success' for r in rows),
            logical_calls=sum(r['logical_calls'] for r in rows),fresh_calls=sum(r['new_calls'] for r in rows),
            failed_calls=sum(r['failed_calls'] for r in rows),generation_errors=sum(r['generation_errors'] for r in rows),
            wall_seconds=s['wall_seconds']))
        comparisons.extend(dict(panel=label,**r) for r in a['comparisons'])
        common.extend(dict(panel=label,**r) for r in a['matched_call_comparisons'])
        effects.extend(dict(panel=label,**r) for r in e)
        target=out/'实验对照'/label;target.mkdir(parents=True,exist_ok=True)
        for filename in ('plan.json','summary.json','analysis.json','results.csv','paired.csv','comparisons.csv',
                         'matched_calls.csv','matched_call_comparisons.csv','costs.csv','curves.csv',
                         'parent_audit.json','parent_events.csv','family_effects.csv'):
            shutil.copyfile(root/filename,target/filename)
    write_csv(out/'实验成本.csv',costs);write_csv(out/'方法对照.csv',comparisons)
    write_csv(out/'共同调用数对照.csv',common);write_csv(out/'邻域收益与成本.csv',effects)
    check=read_json(out/'检查结果.json');replay=read_json(runs/'端到端第十三轮独立复评_v1/summary.json')
    export=read_json(out/'summary.json')
    assert check['coverage']==check['dag_validated_plans']==1500
    assert export['objective_improved']==replay['completed'] and all(r['matches'] and r['new_call'] for r in replay['rows'])
    atomic_json(out/'独立复评.json',replay)
    selection=read_json(runs/'端到端第十三轮计划_v1/selection.json')
    atomic_json(out/'选样规则.json',selection)
    atomic_json(out/'P1机制观察.json',read_json(runs/'端到端第十三轮计划_v1/p1_mechanisms.json'))
    write_csv(out/'P1扩展图特征.csv',selection['p1'])
    portable=read_json(runs/'端到端第十三轮隔离复现_v1/summary.json')
    assert portable['complete'] and all(r['status']=='success' for r in portable['runs'])
    atomic_json(out/'隔离复现.json',portable)
    atomic_json(out/'源码依赖清单.json',read_json(runs/'端到端第十三轮隔离复现_v1/inputs.json'))
    result=dict(panel_runs=sum(r['runs'] for r in costs),panel_fresh_calls=sum(r['fresh_calls'] for r in costs),
        panel_failed_calls=sum(r['failed_calls'] for r in costs),
        scope='development revisions and validation counted separately; failures consume calls; not independent graph count')
    atomic_json(out/'本轮实验汇总.json',result)
    print(result)


if __name__=='__main__':collect()
