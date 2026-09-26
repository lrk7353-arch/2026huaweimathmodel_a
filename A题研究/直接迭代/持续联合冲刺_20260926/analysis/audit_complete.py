"""Read-only audit of all frozen arms, matched seeds and executed mechanisms."""
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from statistics import mean, median
import sys

HERE=Path(__file__).resolve().parents[1]
ROOT=HERE.parent
sys.path.insert(0,str(ROOT))
from common_run import read_json,atomic_json,write_csv,score
from run_persistent_panel import assert_sources,validate_complete
SOURCE=ROOT/'运行结果/持续联合冲刺_20260926/正式75配置_v2_6并发'
AUDIT=HERE/'正式v2完整审计'


@lru_cache(maxsize=48)
def plan(path):
    return read_json(path)


def owners(p):
    cores={t:c for c,ts in enumerate(p['core_schedules']) for t in ts}
    return {o:cores[t] for o,t in p['node_to_subgraph'].items()}


def main():
    manifest=read_json(SOURCE/'execution_manifest.json')
    assert_sources(manifest['sources'])
    imported=read_json(SOURCE/'imported_controls.json')
    for x in imported['imported_controls']:
        for name in ('original_summary','destination_summary'):
            assert hashlib.sha256(Path(x[name]).read_bytes()).hexdigest()==x['summary_sha256']
    checks=read_json(AUDIT/'initialization_checks.json')
    mismatches={r['config_id'] for r in checks if not(r['hashes_equal'] and r['successful_hashes_equal'])}
    rows=list(csv.DictReader((AUDIT/'paired_comparison.csv').open(encoding='utf-8-sig')))
    arms=list(csv.DictReader((AUDIT/'per_arm.csv').open(encoding='utf-8-sig')))
    comparisons=[]
    for p in (1,2,3):
        for b in ('legacy','mature'):
            for group in ('five_core_all','five_core_matched_initialization','frozen_regression','low_core_2_4'):
                if group=='five_core_matched_initialization' and b!='legacy':continue
                rs=[r for r in rows if int(r['problem'])==p and r['baseline']==b and
                    (2<=int(r['cores'])<=4 if group=='low_core_2_4' else int(r['cores'])==5) and
                    (r['group']=='frozen_regression' if group=='frozen_regression' else True) and
                    (r['config_id'] not in mismatches if group=='five_core_matched_initialization' else True)]
                if not rs:continue
                comparisons.append(dict(problem=p,baseline=b,group=group,count=len(rs),
                    old_mean_speedup=mean(float(r['baseline_speedup'])for r in rs),
                    new_mean_speedup=mean(float(r['persistent_speedup'])for r in rs),
                    geomean_speed_gain=math.exp(mean(math.log(float(r['baseline_makespan'])/float(r['persistent_makespan']))for r in rs))-1,
                    wins=sum(r['outcome']=='win'for r in rs),ties=sum(r['outcome']=='tie'for r in rs),losses=sum(r['outcome']=='loss'for r in rs),
                    old_median_seconds=median(float(r['baseline_seconds'])for r in rs),
                    new_median_seconds=median(float(r['persistent_seconds'])for r in rs)))
    costs=[]
    for p in (1,2,3):
        for v in ('mature','legacy','persistent'):
            rs=[r for r in arms if int(r['problem'])==p and r['variant']==v]
            costs.append(dict(problem=p,variant=v,arms=len(rs),valid=sum(r['valid']=='True'for r in rs),
                calls=sum(int(r['calls'])for r in rs),failures=sum(int(r['failures'])for r in rs),
                median_seconds=median(float(r['seconds'])for r in rs),
                logged_generation_median_seconds=median(float(r['logged_generation_seconds'])for r in rs)))
    mechanisms=[];families=defaultdict(Counter);branches=[];observations=[]
    all_ids=[]
    for f in sorted(SOURCE.glob('configurations/*/*/attempt_*/summary.json')):
        s=read_json(f);assert validate_complete(s,24)
        all_ids.extend(c['record']['attempt_id']for c in s['calls'])
        if f.parts[-3]!='persistent':continue
        recs={c['record']['record_path']:c['record'] for c in s['calls']}
        joint=[]
        for i,c in enumerate(s['calls'],1):
            if c.get('phase')!='joint':continue
            rec=c['record'];parent=recs.get(c.get('parent_record'))
            assert parent and parent['status']=='success'
            family=c.get('metadata',{}).get('action','p1_region')
            flags=dict(case=s['case'],problem=s['problem'],cores=s['cores'],call=i,name=c['name'],action=family,
                success=rec['status']=='success',local_time_improved=False,global_accepted=c['accepted'],
                global_time_improved=False,
                moved_ops=None,cross_old_task_blocks=None,partition_changed=None)
            if rec['status']=='success':
                old=plan(parent['plan_path']);new=plan(rec['plan_path']);a=owners(old);b=owners(new)
                flags['moved_ops']=sum(a[o]!=b[o]for o in a)
                flags['local_time_improved']=score(rec)[0]<score(parent)[0]
                prior=[score(x['record'])[0]for x in s['calls'][:i-1]if x['record']['status']=='success']
                flags['global_time_improved']=bool(prior) and score(rec)[0]<min(prior)
                flags.update(parent_time=score(parent)[0],candidate_time=score(rec)[0],copy_delta=score(rec)[1]-score(parent)[1])
                if s['problem']==1:
                    sets=defaultdict(set)
                    oldsets=defaultdict(set);newsets=defaultdict(set)
                    for o,t in new['node_to_subgraph'].items():
                        sets[t].add(old['node_to_subgraph'][o]);oldsets[old['node_to_subgraph'][o]].add(o);newsets[t].add(o)
                    flags['cross_old_task_blocks']=sum(len(v)>1 for v in sets.values())
                    flags['partition_changed']={frozenset(v)for v in oldsets.values()}!={frozenset(v)for v in newsets.values()}
            mechanisms.append(flags);joint.append(flags)
            family_key=f'p{s["problem"]}:{family}'
            families[family_key].update(dict(paid=1,success=int(flags['success']),local_time_improved=int(flags['local_time_improved']),global_accepted=int(flags['global_accepted']),global_time_improved=int(flags['global_time_improved'])))
        visits=[e for e in s.get('branch_events',[])if e['event']=='visit']
        advances=[e for e in s.get('branch_events',[])if e['event']in ('advance','advance_alternate')]
        parent_family=Counter((e.get('parent_record'),e.get('family'))for e in visits)
        gen=[g for g in s['generations']if g.get('name')=='observe_paid_parent']
        obs_counts=Counter(g.get('parent_record')for g in gen)
        branches.append(dict(case=s['case'],problem=s['problem'],cores=s['cores'],
            lineages=len({e.get('lineage')for e in visits}),stale_parent_visits=sum(bool(e.get('stale_parent'))for e in visits),
            max_depth=max([0]+[e.get('depth',0)for e in advances]),
            parent_family_streams_with_multiple_calls=sum(v>1 for v in parent_family.values()),
            max_calls_same_parent_family=max(parent_family.values(),default=0)))
        observations.append(dict(case=s['case'],problem=s['problem'],cores=s['cores'],calls=len(gen),
            repeated_paid_parent_observations=sum(v-1 for v in obs_counts.values()),
            seconds=sum(g['elapsed_seconds']for g in gen),failures=sum(g['status']!='success'for g in gen),
            fallback_count=sum(e['event']=='observation_fallback'for e in s.get('branch_events',[]))))
    assert len(all_ids)==len(set(all_ids))==4901
    write_csv(HERE/'同起点与低核补充对照.csv',comparisons)
    write_csv(HERE/'真实联合动作.csv',mechanisms)
    write_csv(HERE/'分支实际执行.csv',branches)
    write_csv(HERE/'观察成本.csv',observations)
    atomic_json(HERE/'完整接手审计.json',dict(source_revision=manifest['revision'],sources_match=True,formal_unique_calls=len(all_ids),
        imported_controls=len(imported['imported_controls']),imported_calls=sum(x['logical_calls']for x in imported['imported_controls']),
        initialization_mismatches=sorted(mismatches),comparisons=comparisons,costs=costs,
        executed_families={k:dict(v)for k,v in families.items()},max_depth=max(r['max_depth']for r in branches),
        stale_parent_visits=sum(r['stale_parent_visits']for r in branches),
        repeated_paid_parent_observations=sum(r['repeated_paid_parent_observations']for r in observations)))
    print(json.dumps(dict(families={k:dict(v)for k,v in families.items()},max_depth=max(r['max_depth']for r in branches),
        observation_repeats=sum(r['repeated_paid_parent_observations']for r in observations)),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
