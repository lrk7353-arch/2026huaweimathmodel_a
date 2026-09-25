"""One-budget P3 incumbent refinement combining assignment and read ordering."""
from common_run import *
from run_p3_refine import run as legacy
from p3_read_order import run as read_order


def run(case,old,out,budget=12,seconds=120,cores=5,evaluation_dir=None,per_call_timeout=None):
    if key(old)!=(case,3,cores):raise ValueError('incumbent case/problem/core mismatch')
    if budget<1 or seconds<=0:raise ValueError('positive budget/time required')
    if per_call_timeout is not None and per_call_timeout<=0:raise ValueError('per_call_timeout must be positive')
    out=Path(out);out.mkdir(parents=True,exist_ok=False);start=time.monotonic();best=old;calls=[];stages=[]
    initial_cap=min(4,max(1,budget//3))
    for name,fn,cap in [('legacy',legacy,initial_cap),('read_order',read_order,budget)]:
        remaining=budget-len(calls);seconds_left=seconds-(time.monotonic()-start)
        if remaining<=0 or seconds_left<=0:break
        if name=='legacy':s=fn(case,best,None,out/name,min(remaining,cap),seconds_left,cores,evaluation_dir=evaluation_dir,per_call_timeout=per_call_timeout)
        else:s=fn(case,best,out/name,remaining,seconds_left,cores,evaluation_dir=evaluation_dir,per_call_timeout=per_call_timeout)
        calls.extend(dict(t,joint_stage=name) for t in s['calls']);best=s['best_record']
        stages.append(dict(stage=name,before=s['before'],after=s['after'],logical_calls=s['logical_calls']))
        atomic_json(out/'progress.json',dict(case=case,calls=len(calls),after=score(best)[0],stage=name))
    assert len(calls)<=budget
    atomic_json(out/'best.plan.json',read_json(best['plan_path']))
    result=dict(case=case,problem=3,num_cores=cores,before=score(old)[0],after=score(best)[0],best_record=best,
                calls=calls,stages=stages,budget=budget,logical_calls=len(calls),new_calls=sum(not t['record']['cache_hit'] for t in calls),
                elapsed_seconds=time.monotonic()-start,stop_reason='time_budget' if time.monotonic()-start>=seconds else 'budget_or_candidates',
                scope='common warm incumbent, one shared total call/time budget; P2 inheritance not included')
    atomic_json(out/'summary.json',result);return result
