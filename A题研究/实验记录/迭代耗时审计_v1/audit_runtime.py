#!/usr/bin/env python3
"""Read saved completed formal-v2 summaries only; no gzip or evaluation calls."""
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parents[1]
ROOT = RESEARCH / 'advanced_solver/runs/formal_v2'
STRUCTURE = RESEARCH / '方案审阅/结构核验/a_graph_structure.csv'


def sha_bytes(value): return hashlib.sha256(value).hexdigest()
def save(path, value): Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
def read(path): return json.loads(Path(path).read_text(encoding='utf-8'))
def csv_write(path, rows):
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with Path(path).open('w', encoding='utf-8-sig', newline='') as stream:
        w = csv.DictWriter(stream, fieldnames=fields); w.writeheader()
        w.writerows({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in r.items()} for r in rows)
def numeric(value): return type(value) in (int, float)
def total(rows, key): return sum(r[key] for r in rows if numeric(r.get(key)))
def quantiles(values):
    values = sorted(v for v in values if numeric(v))
    if not values: return {'n': 0}
    def q(frac):
        z=(len(values)-1)*frac; i=int(z); return values[i] + (values[min(i+1,len(values)-1)]-values[i])*(z-i)
    return {'n': len(values), 'sum': sum(values), 'mean': statistics.mean(values), 'median': statistics.median(values), 'p90': q(.9), 'p95': q(.95), 'max': values[-1]}
def size_band(count):
    return '01_<=1000' if count <= 1000 else ('02_1001..5000' if count <= 5000 else ('03_5001..10000' if count <= 10000 else '04_>10000'))

def main():
    if (HERE/'summary.json').exists(): raise ValueError('one immutable snapshot only; use a new output directory/script copy for a later audit')
    began = time.perf_counter(); at=datetime.now(timezone.utc).isoformat()
    with STRUCTURE.open(encoding='utf-8-sig', newline='') as f: structures={r['case']:r for r in csv.DictReader(f)}
    batches=[]; frozen=[]; errors=[]; slots=[]; trials=[]; stages=[]
    for directory in sorted(ROOT.iterdir()):
        mp=directory/'manifest.json'
        if not mp.is_file(): continue
        manifest_bytes=mp.read_bytes(); manifest=json.loads(manifest_bytes)
        if 'settings' not in manifest: continue
        settings=manifest['settings']; report=directory/'summary.json'
        if not report.exists(): report=directory/'progress.json'
        if not report.exists(): continue
        b=report.read_bytes(); progress=json.loads(b)
        save(HERE/(directory.name+'.batch_snapshot.json'),progress)
        planned=len(settings['cases'])*len(settings['cores'])*len(settings['problems'])
        complete=[r for r in progress.get('slots',[]) if r.get('feasible') and r.get('search_completed') and r.get('summary_path')]
        batches.append({'batch':directory.name,'profile':settings['profile'],'problems':settings['problems'],'cores':settings['cores'],
            'workers':settings['workers'],'logical_cap':settings['max_evaluations'],'planned_slots':planned,'reported_slots':len(progress.get('slots',[])),
            'completed_feasible_snapshot':len(complete),'finished_batch':report.name=='summary.json','case_coverage':sorted(set(r['case'] for r in complete))})
        frozen.append({'path':str(mp),'sha256':sha_bytes(manifest_bytes)})
        frozen.append({'path':str(report),'sha256':sha_bytes(b),'kind':'mutable progress copied at audit start'})
        for row in complete:
            path=Path(row['summary_path']);raw=path.read_bytes();s=json.loads(raw)
            if not s.get('completed') or s.get('status')!='success':
                errors.append({'path':str(path),'error':'batch claimed completed but selected summary not completed success'}); continue
            launch_path=path.parent.with_name(path.parent.name+'.launch.json')
            launch_bytes=launch_path.read_bytes();launch=json.loads(launch_bytes)
            outer=launch.get('elapsed_seconds')
            slot_file=path.parent.parent/'slot.json'
            verify_interval=(slot_file.stat().st_mtime_ns-launch_path.stat().st_mtime_ns)/1e9 if slot_file.exists() and not row.get('reused') and row.get('attempt_count',1)==1 else None
            structure=structures[row['case']]; count=int(structure['compute_ops'])
            ident={'batch':directory.name,'profile':settings['profile'],'case':row['case'],'problem':s['problem'],'num_cores':s['num_cores'],
                   'compute_ops':count,'size_band':size_band(count)}
            evaluations=s['evaluations']; local=[]; best=None; first_best=None
            final=s['best']['record']['metrics']['makespan']
            for index,t in enumerate(evaluations,1):
                r=t['record'];hit=bool(r.get('cache_hit')); status=r['status'];wrapper=r.get('elapsed_seconds')
                official=r.get('evaluation_elapsed_seconds') if not hit else None
                worker=r.get('worker_elapsed_seconds') if not hit else None
                last=r.get('last_worker_progress',{})
                value=r.get('metrics',{}).get('makespan') if status=='success' else None
                improved=value is not None and (best is None or value<best)
                if value is not None: best=min(best,value) if best is not None else value
                if first_best is None and best==final: first_best=index
                item={**ident,'trial':index,'stage':t['stage'],'name':t['name'],'status':status,'cache_hit':hit,
                    'wrapper_seconds':wrapper,'current_official_function_seconds':official,'current_worker_seconds':worker,
                    'timeout_limit_seconds':r.get('timeout_seconds'),'timeout_observed_worker_lower_seconds':last.get('worker_elapsed_seconds') if status=='timeout' else None,
                    'cache_historical_official_seconds_not_current':r.get('evaluation_elapsed_seconds') if hit else None,
                    'cache_historical_worker_seconds_not_current':r.get('worker_elapsed_seconds') if hit else None,
                    'makespan':value,'prefix_best_time':best,'strict_time_improvement':improved,
                    'result_sha256':r.get('result_sha256'),'plan_sha256':t.get('plan_sha256'),
                    'peak_memory_bytes':r.get('peak_memory_bytes') if not hit else None,
                    'error_type':r.get('error_type'),'error_stage':r.get('error_stage'),'timeout_last_stage':last.get('stage'),
                    'record_path':r.get('record_path')}
                trials.append(item);local.append(item)
            for entry in s['stages']:
                after=entry.get('after'); before=entry.get('before')
                stages.append({**ident,'stage':entry['stage'],'round':entry['round'],'generation_seconds':entry.get('generation_seconds'),
                    'evaluations':entry['evaluation_end']-entry['evaluation_start'],
                    'strict_time_improvement':after is not None and (before is None or after[0]<before[0]),
                    'before_time':before[0] if before else None,'after_time':after[0] if after else None})
            elapsed=s['elapsed_seconds'];generation=sum(e.get('generation_seconds',0) for e in s['stages']);wrapper=total(local,'wrapper_seconds')
            entry={**ident,'logical_calls':len(local),'uncached_calls':sum(not t['cache_hit'] for t in local),'cache_hits':sum(t['cache_hit'] for t in local),
                'timeout_calls':sum(t['status']=='timeout' for t in local),'status_counts':dict(Counter(t['status'] for t in local)),
                'solver_elapsed_seconds':elapsed,'outer_child_elapsed_seconds':outer,
                'post_child_batch_verification_publication_interval_seconds':verify_interval,
                'wrapper_seconds':wrapper,'uncached_wrapper_seconds':sum(t['wrapper_seconds'] for t in local if not t['cache_hit'] and numeric(t['wrapper_seconds'])),
                'cache_lookup_seconds':sum(t['wrapper_seconds'] for t in local if t['cache_hit'] and numeric(t['wrapper_seconds'])),
                'official_function_seconds_completed_uncached':total(local,'current_official_function_seconds'),
                'worker_seconds_completed_uncached':total(local,'current_worker_seconds'),
                'timeout_wrapper_seconds':sum(t['wrapper_seconds'] for t in local if t['status']=='timeout' and numeric(t['wrapper_seconds'])),
                'generation_seconds':generation,'solver_residual_seconds':elapsed-generation-wrapper,
                'outer_minus_solver_seconds':outer-elapsed if numeric(outer) else None,
                'summary_bytes':len(raw),'final_time':final,'first_final_time_trial':first_best,'best_stage':s['best']['stage'],
                'duplicates_skipped':len(s.get('duplicates',[])),'generation_failures':len(s.get('generation_failures',[])),
                'summary_path':str(path),'summary_sha256':sha_bytes(raw),'launch_sha256':sha_bytes(launch_bytes)}
            for cap in (4,8,12,16,24):
                upto=local[:cap];pbest=upto[-1]['prefix_best_time'] if upto else None
                entry[f'prefix{cap}_time']=pbest
                entry[f'prefix{cap}_gap_to_final']=pbest/final-1 if pbest else None
                entry[f'prefix{cap}_omitted_calls']=max(0,len(local)-cap)
                entry[f'prefix{cap}_saved_wrapper_seconds_hindsight']=sum(t['wrapper_seconds'] for t in local[cap:] if numeric(t['wrapper_seconds']))
            slots.append(entry)
            if sha_bytes(path.read_bytes())!=entry['summary_sha256'] or sha_bytes(launch_path.read_bytes())!=entry['launch_sha256']:
                errors.append({'path':str(path),'error':'completed evidence changed during read'})
    def aggregate(values):
        if not values: return {}
        sums=['logical_calls','uncached_calls','cache_hits','timeout_calls','solver_elapsed_seconds','outer_child_elapsed_seconds',
              'wrapper_seconds','uncached_wrapper_seconds','cache_lookup_seconds','official_function_seconds_completed_uncached','worker_seconds_completed_uncached',
              'timeout_wrapper_seconds','generation_seconds','solver_residual_seconds','outer_minus_solver_seconds','post_child_batch_verification_publication_interval_seconds']
        totals={k:total(values,k) for k in sums}
        return {'slots':len(values),'unique_cases':len(set(v['case'] for v in values)),**totals,
            'cache_hit_fraction':totals['cache_hits']/totals['logical_calls'] if totals['logical_calls'] else None,
            'per_slot':{k:quantiles(v.get(k) for v in values) for k in ['logical_calls','solver_elapsed_seconds','outer_child_elapsed_seconds','generation_seconds','solver_residual_seconds','post_child_batch_verification_publication_interval_seconds']},
            'first_final_time_trial':quantiles(v['first_final_time_trial'] for v in values),'best_stage_counts':dict(Counter(v['best_stage'] for v in values))}
    grouped=[]
    for scope,subset in [('all_completed_profiles',slots),('full_profile_only',[r for r in slots if r['profile']=='full'])]:
        for p in (1,2,3):
            values=[r for r in subset if r['problem']==p]
            grouped.append({'scope':scope,'problem':p,'size_band':'all',**aggregate(values)})
            for band in sorted(set(r['size_band'] for r in subset)):
                values=[r for r in subset if r['problem']==p and r['size_band']==band]
                if values: grouped.append({'scope':scope,'problem':p,'size_band':band,**aggregate(values)})
    stagegroups=[]
    for p in (1,2,3):
        for name in sorted(set(t['stage'] for t in trials)):
            t=[x for x in trials if x['problem']==p and x['profile']=='full' and x['stage']==name]
            st=[x for x in stages if x['problem']==p and x['profile']=='full' and x['stage']==name]
            if not t and not st: continue
            stagegroups.append({'problem':p,'stage':name,'calls':len(t),'cache_hits':sum(x['cache_hit'] for x in t),
                'timeout_calls':sum(x['status']=='timeout' for x in t),'wrapper_seconds':total(t,'wrapper_seconds'),
                'completed_uncached_official_seconds':total(t,'current_official_function_seconds'),
                'generation_seconds':total(st,'generation_seconds'),'generator_invocations':len(st),
                'zero_call_generator_invocations':sum(not x['evaluations'] for x in st),
                'zero_call_generation_seconds':sum(x['generation_seconds'] for x in st if not x['evaluations']),
                'slots_with_any_stage_time_improvement':len(set((x['batch'],x['case'],x['num_cores']) for x in t if x['strict_time_improvement'])),
                'time_improving_calls':sum(x['strict_time_improvement'] for x in t)})
    prefixes=[]
    for p in (1,2,3):
        values=[r for r in slots if r['profile']=='full' and r['problem']==p]
        for cap in (4,8,12,16):
            gaps=[r[f'prefix{cap}_gap_to_final'] for r in values if r[f'prefix{cap}_gap_to_final'] is not None]
            prefixes.append({'problem':p,'cap':cap,'slots':len(values),'prefix_feasible_slots':len(gaps),
                'same_final_time':sum(g==0 for g in gaps),'within_one_percent':sum(g<=.01 for g in gaps),
                'gap_to_final':quantiles(gaps),'omitted_calls':total(values,f'prefix{cap}_omitted_calls'),
                'saved_wrapper_seconds_hindsight':total(values,f'prefix{cap}_saved_wrapper_seconds_hindsight')})
    bycase=[]
    for case in sorted(set(s['case'] for s in slots)):
        values=[s for s in slots if s['case']==case and s['profile']=='full']
        if values: bycase.append({'case':case,'compute_ops':values[0]['compute_ops'],**aggregate(values)})
    timeoutrows=[t for t in trials if t['status']=='timeout']
    summary={'snapshot_started_at_utc':at,'snapshot_finished_at_utc':datetime.now(timezone.utc).isoformat(),
        'analysis_wall_seconds':time.perf_counter()-began,'source_script_sha256':sha_bytes(Path(__file__).read_bytes()),
        'structure_csv_sha256':sha_bytes(STRUCTURE.read_bytes()),'input_batch_snapshots':frozen,'batch_coverage':batches,
        'completed_slots_audited':len(slots),'logical_trials_audited':len(trials),'errors':errors,
        'full_profile_coverage':{str(p):len([s for s in slots if s['profile']=='full' and s['problem']==p]) for p in (1,2,3)},
        'time_semantics':{'solver':'engine elapsed covers input/IR, generation, wrapper calls, checkpoint serialization; excludes batch post-child raw validation',
            'outer_child':'launch.elapsed_seconds includes solver subprocess lifetime; excludes batch post-child validation',
            'post_child_validation_proxy':'slot.json mtime minus updated launch.json mtime, new unreused attempt only; file-publication interval, not profiler CPU time',
            'worker':'current uncached calls only; includes input checks/loading, official function and gzip output save; missing on timeout',
            'official':'current uncached calls only; official function wall duration, not CPU time; excludes timeouts unless recorded',
            'cache':'cache-hit worker/evaluation durations are copied historical durations, never current wall work',
            'sum':'sum of occupied durations across concurrent calls is not campaign wallclock',
            'residual':'solver minus generation minus wrapper; includes plan validation/hashing, checkpoints and bookkeeping, cannot assign exactly without profiler'},
        'limitations':['Snapshot excludes currently running/incomplete slots and unreported attempts; large slow cases and late P3 cases are censored.',
            'Partial component comparator kept separate from full profile. No gzip reread or new correctness/replay validation.',
            'Prefix comparisons are retrospective savings/gaps on existing evaluation order, not rerun results or an adaptive stopping guarantee.',
            'Shared exact cache and concurrent CPU contention prevent cold standalone speed claims. No GPU extrapolation.'],
        'total':aggregate(slots),'full_only_total':aggregate([s for s in slots if s['profile']=='full']),
        'problem_size_aggregates':grouped,'full_stage_aggregates':stagegroups,'prefix_sensitivity':prefixes,
        'top_20_cases_by_outer_child_seconds':sorted(bycase,key=lambda r:r['outer_child_elapsed_seconds'],reverse=True)[:20],
        'top_20_slots_by_outer_child_seconds':sorted(slots,key=lambda r:r.get('outer_child_elapsed_seconds') or 0,reverse=True)[:20],
        'timeout_trials':timeoutrows}
    csv_write(HERE/'slots.csv',slots);csv_write(HERE/'trials.csv',trials);csv_write(HERE/'stages.csv',stages)
    save(HERE/'summary.json',summary)
    print(json.dumps({'completed_slots':len(slots),'trials':len(trials),'coverage':summary['full_profile_coverage'],'analysis_seconds':summary['analysis_wall_seconds'],'errors':errors},ensure_ascii=False))

if __name__=='__main__':main()
