"""Read-only post-hoc runtime/traffic analysis of completed formal v2 summaries."""
from pathlib import Path
import collections
import hashlib
import json
import statistics

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
BASE = ROOT / 'A题研究/advanced_solver/runs/formal_v2'

def dump(name, value):
    (OUT/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')

def quantile(xs, p):
    xs=sorted(xs)
    if not xs: return None
    t=(len(xs)-1)*p; i=int(t)
    return xs[i] if i==len(xs)-1 else xs[i]+(xs[i+1]-xs[i])*(t-i)

def group_stats(rows):
    return {'count':len(rows), 'status':dict(collections.Counter(r['status'] for r in rows)),
      'fresh_calls':sum(not r['cache_hit'] for r in rows), 'cache_hits':sum(r['cache_hit'] for r in rows),
      'wrapper_seconds_sum':sum(r['wrapper_seconds'] for r in rows),
      'fresh_success_official_seconds_sum':sum((r['official_seconds'] or 0) for r in rows if not r['cache_hit'] and r['status']=='success'),
      'timeout_wrapper_seconds_sum':sum(r['wrapper_seconds'] for r in rows if r['status']=='timeout'),
      'strict_makespan_improvement_events':sum(r['strict_improvement'] for r in rows),
      'lexicographic_improvement_events':sum(r['lex_improvement'] for r in rows),
      'selected_final_plan_count':sum(r['selected'] for r in rows)}

features={}
for p in sorted((ROOT/'选题分析/A题附件/data').glob('case_*.json')):
    d=json.loads(p.read_text())
    features[p.stem]={'compute_ops':sum(op['pipe'] in ('PIPE_M','PIPE_V') for op in d['ops']),
                     'total_ops':len(d['ops']), 'graph_sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
rows=[];slots=[];sources=[];stagerows=[]
for problem in (1,2,3):
    paths=sorted((BASE/f'full_p{problem}_seed17/slots').glob('*/*/attempt_*/summary.json'))
    assert len(paths)==400, (problem,len(paths))
    for path in paths:
        raw=path.read_bytes();d=json.loads(raw);case=d['case'];core=d['num_cores'];key=f'{case}_p{problem}_n{core}'
        assert d['completed'] and d['status']=='success'
        assert d['graph_sha256']==features[case]['graph_sha256']
        ss=json.loads((path.parent.parent/'slot.json').read_text())
        assert ss['outcome']=='completed_feasible' and ss['summary_path']==str(path)
        sources.append({'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()})
        bestkey=None;componentkey=None;evalrows=[]
        for j,e in enumerate(d['evaluations']):
            r=e['record'];m=r.get('metrics') or {};traffic=m.get('data_movement_bytes') or {}
            score=(m['makespan'],traffic['added_copy_bytes']) if r['status']=='success' else None
            strict=bool(score is not None and bestkey is not None and score[0]<bestkey[0])
            lexi=bool(score is not None and bestkey is not None and score<bestkey)
            rr={'slot':key,'case':case,'problem':problem,'cores':core,'ordinal':j+1,'stage':e['stage'],
                'name':e['name'],'family':e.get('metadata',{}).get('family','unspecified'),
                'status':r['status'],'cache_hit':bool(r.get('cache_hit',False)),
                'wrapper_seconds':r['elapsed_seconds'],'official_seconds':r.get('evaluation_elapsed_seconds'),
                'worker_seconds':r.get('worker_elapsed_seconds'),'strict_improvement':strict,'lex_improvement':lexi,
                'selected':e['plan_sha256']==d['best']['plan_sha256'], 'score':score,
                'traffic':traffic, 'compute_ops':features[case]['compute_ops'],
                'attempt_id':r['attempt_id'],'result_path':r.get('result_path'),'plan_sha256':e['plan_sha256']}
            evalrows.append(rr);rows.append(rr)
            if score is not None and (bestkey is None or score<bestkey):bestkey=score
            if score is not None and e['stage']=='component' and (componentkey is None or score<componentkey):componentkey=score
        assert len(evalrows)==d['evaluated_count']
        assert sum(not r['cache_hit'] for r in evalrows)==d['official_calls']
        assert sum(r['cache_hit'] for r in evalrows)==d['cache_hits']
        best=d['best']['record']['metrics'];bf=best['data_movement_bytes']
        assert bestkey==(best['makespan'],bf['added_copy_bytes'])
        stage_detail=[]
        for st in d['stages']:
            before=st.get('before');after=st.get('after')
            sr={'slot':key,'problem':problem,'stage':st['stage'],'round':st['round'],
                'generation_seconds':st.get('generation_seconds',0),
                'before':before,'after':after,'evaluation_start':st['evaluation_start'],'evaluation_end':st['evaluation_end'],
                'strict_improvement':bool(before and after and after[0]<before[0]),
                'relative_reduction':(1-after[0]/before[0]) if before and after else None}
            stagerows.append(sr);stage_detail.append(sr)
        prefix={}
        for n in [4,8,12,20,24,32]:
            scores=[r['score'] for r in evalrows[:n] if r['score']]
            if scores:
                pk=min(scores)
                prefix[str(n)]={'same_makespan_as_final':pk[0]==bestkey[0], 'same_lex_score_as_final':pk==bestkey,
                  'makespan_regret_fraction':pk[0]/bestkey[0]-1,
                  'post_prefix_wrapper_seconds':sum(r['wrapper_seconds'] for r in evalrows[n:])}
        componentrow=min((r for r in evalrows if r['stage']=='component' and r['score']),key=lambda r:r['score'])
        slots.append({'slot':key,'case':case,'problem':problem,'cores':core,
           'elapsed_seconds':d['elapsed_seconds'],'wrapper_seconds':sum(r['wrapper_seconds'] for r in evalrows),
           'generation_seconds':sum(s['generation_seconds'] for s in stage_detail),
           'official_calls':d['official_calls'],'cache_hits':d['cache_hits'],'status_counts':d['status_counts'],
           'compute_ops':features[case]['compute_ops'],'best_stage':d['best']['stage'],'best_name':d['best']['name'],
           'best_makespan':bestkey[0],'best_traffic':bf,'component_makespan':componentkey[0],
           'component_traffic':componentrow['traffic'],'improved_over_component':bestkey[0]<componentkey[0],
           'reduction_vs_component':1-bestkey[0]/componentkey[0],
           'prefix':prefix})
reports={}
for problem in (1,2,3):
    rs=[r for r in rows if r['problem']==problem];ss=[s for s in slots if s['problem']==problem]
    perstage={stage:group_stats([r for r in rs if r['stage']==stage]) for stage in sorted({r['stage'] for r in rs})}
    perfamily={f:group_stats([r for r in rs if r['family']==f]) for f in sorted({r['family'] for r in rs})}
    pername={f:group_stats([r for r in rs if r['name']==f]) for f in sorted({r['name'] for r in rs})}
    tail=sorted(ss,key=lambda s:s['elapsed_seconds'],reverse=True)
    large=[s for s in ss if s['compute_ops']>10000]
    traffic_compare=[]
    for s in ss:
        if s['improved_over_component']:
            b=s['best_traffic'];c=s['component_traffic']
            traffic_compare.append({'slot':s['slot'],'reduction':s['reduction_vs_component'],
                'added_copy_delta':b['added_copy_bytes']-c['added_copy_bytes'],
                'spill_delta':b['spill_added_copy_bytes']-c['spill_added_copy_bytes'],
                'partition_delta':b['partition_added_copy_bytes']-c['partition_added_copy_bytes']})
    elapsed=sum(s['elapsed_seconds'] for s in ss)
    reports[str(problem)]={'slots':400,**group_stats(rs),
       'solver_elapsed_seconds_sum':elapsed,'generation_seconds_sum':sum(s['generation_seconds'] for s in ss),
       'solver_elapsed_quantiles':{str(q):quantile([s['elapsed_seconds'] for s in ss],q) for q in [0,.5,.9,.95,1]},
       'large_compute_gt10000':{'graphs':len({s['case'] for s in large}),'slots':len(large),
           'solver_elapsed_seconds_sum':sum(s['elapsed_seconds'] for s in large),
           'solver_elapsed_fraction':sum(s['elapsed_seconds'] for s in large)/elapsed},
       'per_stage':perstage,'per_family':perfamily,'per_name':pername,
       'slowest_slots':tail[:12],
       'prefix_hindsight':{str(n):{'same_makespan':sum(s['prefix'][str(n)]['same_makespan_as_final'] for s in ss),
           'same_lex_score':sum(s['prefix'][str(n)]['same_lex_score_as_final'] for s in ss),
           'post_prefix_wrapper_seconds':sum(s['prefix'][str(n)]['post_prefix_wrapper_seconds'] for s in ss),
           'max_makespan_regret_fraction':max(s['prefix'][str(n)]['makespan_regret_fraction'] for s in ss)} for n in [4,8,12,20,24,32]},
       'traffic_among_improved_over_component':{'n':len(traffic_compare),
           'more_added_copy':sum(r['added_copy_delta']>0 for r in traffic_compare),
           'less_added_copy':sum(r['added_copy_delta']<0 for r in traffic_compare),
           'same_added_copy':sum(r['added_copy_delta']==0 for r in traffic_compare),
           'rows':traffic_compare},
       'best_final_traffic_totals':{k:sum(s['best_traffic'][k] for s in ss) for k in ss[0]['best_traffic']},
       'best_final_added_copy_positive':sum(s['best_traffic']['added_copy_bytes']>0 for s in ss),
       'best_final_spill_positive':sum(s['best_traffic']['spill_added_copy_bytes']>0 for s in ss)}
dump('runtime_analysis.json',{'scope':'formal_v2 full P1/P2/P3 seed17, each 100 graphs × cores 2..5',
    'caveats':['All accumulated seconds are sums over overlapping processes, NOT end-to-end wall-clock duration.',
    'Cache-hit evaluation_elapsed_seconds describes originating old run and is excluded from fresh official sum.',
    'Prefix and candidate-family outcomes are post-hoc observations, not validated stopping/pruning rules.',
    'Stage outcomes are sequential additions, not equal-budget randomized causal ablations.',
    'Source summaries and graph identities checked; parent task independently audits full raw results.'],
    'problems':reports})
dump('slot_runtime_traffic.json',slots)
dump('evaluation_runtime.json',rows)
dump('source_manifest.json',sources)
dump('stage_transitions.json',stagerows)
print(json.dumps({k:{kk:v[kk] for kk in ['slots','count','fresh_calls','cache_hits','status','solver_elapsed_seconds_sum','wrapper_seconds_sum','generation_seconds_sum','large_compute_gt10000','per_stage']} for k,v in reports.items()},ensure_ascii=False,indent=2))
