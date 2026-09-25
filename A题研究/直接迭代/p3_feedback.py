"""Shared-budget P3 candidate queues with exact deduplication and trace feedback.

This is an incumbent refiner. The caller owns initial-plan evaluation. Both
arms use the same official best; cached results still consume logical calls.
Only exact ordered serialization is deduplicated, never relabelled plans.
"""
import gzip
from collections import defaultdict,deque
from common_run import *
from advanced_solver.cache_refine import generate_cache_candidates
from p3_read_order import generate as read_candidates


def exact(plan):
    return json.dumps(plan,ensure_ascii=False,separators=(',',':'))


def diversify(candidates):
    controls=[c for c in candidates if c['metadata']['is_reencoding_control']]
    groups=defaultdict(list)
    for c in candidates:
        if not c['metadata']['is_reencoding_control']:groups[c['metadata']['mechanism']].append(c)
    return controls+[g[i] for i in range(max(map(len,groups.values()),default=0)) for g in groups.values() if i<len(g)]


def choose_arm(available,stats,policy,total):
    """Small probes followed by measured gain per estimated evaluation second.

    Cold evaluation cost is used even on cache hits, so a cache lookup's tiny
    wall time does not become a fictitious cheap arm. It remains an estimate.
    """
    if len(available)==1:return available[0]
    if policy=='interleave':
        return min(available,key=lambda a:(stats[a]['calls'],a!='legacy'))
    # Four actual distinct probes per arm; exhausted arms automatically return
    # their allowance. Budgets below eight naturally stop during this phase.
    for arm in ('legacy','read_order'):
        if arm in available and stats[arm]['calls']<4:return arm
    # Do not completely starve a productive alternative in longer runs.
    starved=[a for a in available if total-stats[a]['last_call']>=6]
    if starved:return min(starved,key=lambda a:stats[a]['last_call'])
    def priority(a):
        s=stats[a]
        return (s['gain']/max(.001,s['estimated_seconds']),a=='legacy')
    return max(available,key=priority)


def run(case,old,out,budget=12,seconds=120,cores=5,policy='feedback',evaluation_dir=None,seed=17,seen_signatures=None):
    if key(old)!=(case,3,cores):raise ValueError('incumbent case/problem/core mismatch')
    if budget<1 or seconds<=0 or policy not in ('feedback','interleave'):raise ValueError('invalid budget/time/policy')
    out=Path(out);out.mkdir(parents=True,exist_ok=False);started=time.monotonic();deadline=started+seconds
    evaldir=Path(evaluation_dir) if evaluation_dir is not None else R/'advanced_solver/runs/formal_v2/evaluations'
    ir=GraphIR.from_path(DATA/(case+'.json'));best=old;initial=read_json(old['plan_path'])
    seen=set(seen_signatures or ())|{exact(initial)};calls=[];events=[];generation=[];queues={a:deque() for a in ('legacy','read_order')}
    stats={a:dict(calls=0,gain=0.,estimated_seconds=0.,last_call=-1) for a in queues}
    legacy_round=-1;legacy_guided=0;read_generations=0;read_source_time=None;read_source_signature=None
    generation_seconds=0.;evaluation_wall_seconds=0.;refresh_pending=False

    def generate(arm,reason):
        nonlocal legacy_round,legacy_guided,read_generations,read_source_time,read_source_signature,generation_seconds,refresh_pending
        if time.monotonic()>=deadline:return
        t=time.monotonic();plan=read_json(best['plan_path'])
        with gzip.open(best['result_path'],'rt') as f:raw=json.load(f)
        if arm=='legacy':
            legacy_round+=1;legacy_guided=0
            cs,diag=generate_cache_candidates(ir,plan,raw,num_cores=cores,max_candidates=12,round_index=legacy_round,seed=seed)
            cs=diversify(cs);batch=legacy_round
        else:
            cs,diag=read_candidates(ir,plan,raw,cores,limit=max(12,budget))
            read_generations+=1;batch=read_generations-1;read_source_time=score(best)[0]
            read_source_signature=exact(plan);refresh_pending=False
        elapsed=time.monotonic()-t;generation_seconds+=elapsed
        info=dict(arm=arm,batch=batch,reason=reason,source_makespan=score(best)[0],source_record=best['record_path'],
                  candidate_count=len(cs),elapsed_seconds=elapsed)
        generation.append(info)
        atomic_json(out/f'{arm}_generation_{batch}.json',dict(**info,diagnostics=diag,candidates=[dict(name=c['name'],metadata=c['metadata']) for c in cs]))
        items=[dict(c,arm=arm,batch=batch,source_record=best['record_path']) for c in cs]
        # A refresh puts new proposals first, but retains untried older plans.
        queues[arm].extendleft(reversed(items))

    def next_candidate(arm):
        nonlocal refresh_pending
        while time.monotonic()<deadline:
            if arm=='legacy' and legacy_round==0 and legacy_guided>=3:
                queues[arm].clear()  # Match legacy's round-0 width, then resume round 1.
            if arm=='read_order' and refresh_pending and read_generations<2:
                generate(arm,'accepted_gain_trace_refresh')
            if not queues[arm]:
                if arm=='legacy' and legacy_round<1:generate(arm,'initial' if legacy_round<0 else 'legacy_round_transition')
                elif arm=='read_order' and read_generations==0:generate(arm,'initial')
                else:return None
            if not queues[arm]:return None
            c=queues[arm][0];signature=exact(c['plan'])
            if signature in seen:
                queues[arm].popleft();events.append(dict(event='duplicate_skip',arm=arm,name=c['name'],batch=c['batch']))
                continue
            return c
        return None

    while len(calls)<budget and time.monotonic()<deadline:
        # Lazy generation: initialize the mature neighbourhood before paying
        # for a second generator. Then maintain both resumable queues.
        preferred='legacy' if stats['legacy']['calls']<min(4,budget) and policy=='feedback' else None
        candidates={}
        if preferred:
            c=next_candidate(preferred)
            if c:candidates[preferred]=c
        if not candidates or preferred is None:
            for arm in queues:
                c=next_candidate(arm)
                if c:candidates[arm]=c
        if not candidates:break
        arm=choose_arm(list(candidates),stats,policy,len(calls));c=candidates[arm]
        queues[arm].popleft();signature=exact(c['plan'])
        if signature in seen:continue
        remaining=deadline-time.monotonic()
        if remaining<=0:break
        validate_plan(ir,c['plan']);seen.add(signature);previous=score(best)
        # Record the allocation reason and queue state before each evaluation.
        decision=dict(event='allocation',call=len(calls)+1,arm=arm,name=c['name'],batch=c['batch'],
                      available=list(candidates),stats={k:dict(v) for k,v in stats.items()})
        events.append(decision);t=time.monotonic()
        rec=evaluate(DATA/(case+'.json'),c['plan'],3,evaldir,timeout=min(60,remaining),config_path=DATA/'config.txt')
        wall=time.monotonic()-t;evaluation_wall_seconds+=wall
        accepted=rec['status']=='success' and score(rec)<previous
        gain=(previous[0]-score(rec)[0])/max(1,score(old)[0]) if accepted else 0.
        if accepted:best=rec;atomic_json(out/'best.plan.json',c['plan'])
        measured=rec.get('evaluation_elapsed_seconds')
        if not isinstance(measured,(int,float)) or measured<=0:measured=wall
        stats[arm]['calls']+=1;stats[arm]['gain']+=max(0,gain)
        stats[arm]['estimated_seconds']+=max(.001,measured);stats[arm]['last_call']=len(calls)+1
        if arm=='legacy' and c['batch']==0 and not c['metadata']['is_reencoding_control']:legacy_guided+=1
        calls.append(dict(name=c['name'],arm=arm,batch=c['batch'],metadata=c['metadata'],source_record=c['source_record'],
                          record=rec,accepted=accepted,relative_gain=gain,evaluation_wall_seconds=wall,
                          estimated_evaluation_seconds=measured,elapsed_seconds=time.monotonic()-started))
        if policy=='feedback' and accepted and read_generations==1 and read_source_time is not None:
            if score(best)[0]<=.995*read_source_time and exact(read_json(best['plan_path']))!=read_source_signature:
                refresh_pending=True
        atomic_json(out/'progress.json',dict(case=case,policy=policy,calls=len(calls),before=score(old)[0],after=score(best)[0],stats=stats))
    atomic_json(out/'best.plan.json',read_json(best['plan_path']))
    result=dict(case=case,problem=3,num_cores=cores,policy=policy,seed=seed,before=score(old)[0],after=score(best)[0],best_record=best,
        budget=budget,calls=calls,logical_calls=len(calls),new_calls=sum(not t['record']['cache_hit'] for t in calls),
        elapsed_seconds=time.monotonic()-started,generation_seconds=generation_seconds,evaluation_wall_seconds=evaluation_wall_seconds,
        stats=stats,routing=events,generations=generation,evaluation_dir=str(evaldir),
        stop_reason='time_budget' if time.monotonic()>=deadline else 'budget_or_candidates',
        scope='shared warm incumbent; exact ordered-plan dedup; same total call cap; cache-origin cost is a heuristic, not a cold runtime claim')
    atomic_json(out/'summary.json',result);return result
