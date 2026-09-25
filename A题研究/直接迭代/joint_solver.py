"""Joint v3: preserve a charged strong prefix, then serve retained candidate queues.

The prefix is the unchanged teammate B12 algorithm. Tail queues keep old parents
reachable for a two-call lease; an improvement >=1% may enqueue one new parent.
All original-graph work, generation, failures and evaluations share one budget.
"""
import argparse, gzip, hashlib, json, math, time
from collections import defaultdict, deque
from pathlib import Path
from common_run import DATA, GraphIR, atomic_json, read_json, score, validate_plan, evaluate
from unified_structure import Structure, candidate_stream

def exact(plan):
    return json.dumps(plan,ensure_ascii=False,separators=(",",":"))

def strong(case,problem,cores,out,budget,seconds,timeout):
    if problem==1:
        from cold_portfolio import run
        return run(case,problem,cores,"integrated",out,budget,seconds,timeout)
    from p23_pipeline import run
    return run(case,problem,cores,"trace_routed",out,budget,seconds,
               evaluation_timeout=timeout,evaluation_dir=Path(out)/"evaluations")

class RetainedQueues:
    """Pending parents remain runnable after best changes; no beam eviction."""
    def __init__(self,lease=2):
        self.lease=lease;self.queues=defaultdict(deque);self.keys=set()
    def add(self,family,parent,iterator):
        key=family,parent
        if key not in self.keys:
            self.keys.add(key);self.queues[family].append(dict(parent=parent,iterator=iter(iterator),served=0))
    def pop(self,family):
        pool=self.queues[family]
        while pool:
            q=pool[0]
            try:return next(q["iterator"]),q["parent"]
            except StopIteration:pool.popleft()
        return None,None
    def charged(self,family,parent):
        pool=self.queues[family]
        if not pool:return
        q=pool[0]
        if q["parent"]!=parent:raise AssertionError("queue changed during evaluation")
        q["served"]+=1
        if q["served"]>=self.lease and len(pool)>1:
            q["served"]=0;pool.rotate(-1)

def tail_order(structure,record,problem):
    if problem==1:return ["structure","critical","joint"],{"reason":"P1_structural_complement"}
    if structure.profile["dominant_fraction"]>=.6:
        return ["structure","region","data","legacy"],{"reason":"dominant_compute_component"}
    from cold_portfolio import local_order
    order,diag=local_order(structure.ir,record,problem,"integrated")
    return [f for f in order if f!="structure"]+["wcc","structure"],diag

def critical_candidates(ir,plan,raw,cores,deadline,diagnostics):
    from p1_bottleneck_diagnose import diagnose,regions
    from p1_bottleneck_repartition import generate
    from p1_task_refine import view
    diag=diagnose(ir,plan,raw)
    mapping,owner,nodes,edges,pred=view(ir,plan)
    tasks={t["task"]:t for t in diag["heavy_tasks"]}
    ranked=sorted(diag["final_blocker_chain"],key=lambda t:(-tasks[t]["duration"],t))
    selected=[]
    for seed in ranked[:2]:
        region={seed};count=len(nodes[seed])
        if count>8192:continue
        neighbours=sorted(edges[seed]|pred[seed],key=lambda t:(-tasks[t]["duration"],t))
        for t in neighbours:
            if count+len(nodes[t])<=8192:
                region.add(t);count+=len(nodes[t])
        if sorted(region) not in selected:selected.append(sorted(region))
    if not selected:selected=regions(ir,plan,diag,limit=1)
    candidates,info=generate(ir,plan,selected,seconds=max(.001,min(8.,deadline-time.monotonic())))
    diagnostics.append(dict(family="critical",targets=selected,trace_diagnosis=diag,generation=info))
    # Round-robin by mechanism before width alternatives; all are complete plans.
    pools=defaultdict(list)
    for c in candidates:pools[c["metadata"]["family"]].append(c)
    return [g[i] for i in range(max(map(len,pools.values()),default=0)) for g in pools.values() if i<len(g)]

def proposals(family,record,structure,problem,cores,deadline,diagnostics):
    if family=="structure":
        yield from candidate_stream(structure,problem,cores,deadline);return
    plan=read_json(record["plan_path"])
    with gzip.open(record["result_path"],"rt") as f:raw=json.load(f)
    if family=="critical":
        yield from critical_candidates(structure.ir,plan,raw,cores,deadline,diagnostics);return
    if family=="wcc":
        from controller import generate_wcc_candidates
        cs,diag=generate_wcc_candidates(structure.ir,plan,num_cores=cores,max_candidates=16,seed=17,policy="mixed")
    else:
        from refine_regions import candidates
        cs,diag=candidates(structure.ir,plan,raw,problem,cores,family,0,16)
    diagnostics.append(dict(family=family,parent=record["record_path"],details=diag))
    yield from cs

def run(case,problem,cores,out,budget=16,seconds=180,evaluation_timeout=45,variant="joint"):
    if variant not in ("joint","strong"):raise ValueError("joint or strong required")
    if problem not in (1,2,3) or type(cores) is not int or cores not in range(1,6):raise ValueError("invalid scenario/core count")
    if type(budget) is not int or budget<1 or not all(math.isfinite(x) and x>0 for x in (seconds,evaluation_timeout)):raise ValueError("positive finite budgets required")
    out=Path(out).resolve()
    if out==DATA or DATA in out.parents:raise ValueError("official inputs are read-only")
    out.mkdir(parents=True,exist_ok=False);started=time.monotonic();deadline=started+seconds
    atomic_json(out/"input.json",dict(case=case,problem=problem,cores=cores,budget=budget,seconds=seconds,
        evaluation_timeout=evaluation_timeout,variant=variant,prefix_cap=min(12,budget) if variant=="joint" else budget,
        seed=17,refresh_gain=.01,queue_lease=2))
    prefix=strong(case,problem,cores,out/"prefix",min(12,budget) if variant=="joint" else budget,
                  max(.001,deadline-time.monotonic()),evaluation_timeout)
    calls=[dict(c,joint_stage="protected_prefix") for c in prefix.get("calls",prefix.get("evaluations",[]))]
    assert len(calls)==prefix["logical_calls"] and len(calls)<=budget
    best=prefix["best_record"];seen=set()
    for c in calls:
        path=c["record"].get("plan_path")
        if path:seen.add(exact(read_json(path)))
    diagnostics=[];skipped=[];generation_seconds=0.;queues=RetainedQueues(2)
    refresh_count=0;last_refresh_span=score(best)[0] if best else None
    if best:atomic_json(out/"best.plan.json",read_json(best["plan_path"]))
    def available():return len(calls)<budget and time.monotonic()<deadline
    if variant=="joint" and available():
        t=time.monotonic();ir=GraphIR.from_path(DATA/(case+".json"));structure=Structure(ir)
        order,decision=tail_order(structure,best,problem) if best else (["structure"],{"reason":"prefix_no_success"})
        diagnostics.append(dict(family="tail_route",order=order,decision=decision))
        for family in order:
            parent=prefix.get("best_component") if family=="wcc" else best
            if parent is None and family!="structure":continue
            parent_key=parent["record_path"] if parent else "original_graph"
            queues.add(family,parent_key,proposals(family,parent,structure,problem,cores,deadline,diagnostics))
        generation_seconds+=time.monotonic()-t
        cursor=0;dry=0;attempted=0
        while available() and dry<len(order):
            family=order[cursor%len(order)];cursor+=1;t=time.monotonic()
            candidate=None;parent=None
            try:
                while available():
                    candidate,parent=queues.pop(family)
                    if candidate is None:break
                    sig=exact(candidate["plan"]);attempted+=1
                    if sig in seen:
                        skipped.append(dict(name=candidate["name"],reason="exact_order_duplicate"));continue
                    validate_plan(ir,candidate["plan"])
                    if len(candidate["plan"]["core_schedules"])!=cores:raise ValueError("candidate core mismatch")
                    # Dedup preserves insertion order. Proxies never prune.
                    seen.add(sig);break
                else:candidate=None
            except (ValueError,KeyError,TimeoutError) as exc:
                diagnostics.append(dict(family=family,error=repr(exc)));candidate=None
            generation_seconds+=time.monotonic()-t
            if not candidate:
                dry+=1;continue
            dry=0
            if not available():break
            trial=dict(name=candidate["name"],phase=family,parent_record=parent,metadata=candidate.get("metadata",{}),
                       joint_stage="retained_tail",accepted=False)
            calls.append(trial)
            try:record=evaluate(DATA/(case+".json"),candidate["plan"],problem,out/"evaluations",
                               timeout=min(evaluation_timeout,max(.001,deadline-time.monotonic())),config_path=DATA/"config.txt")
            except Exception as exc:record=dict(status="wrapper_exception",cache_hit=False,error=repr(exc))
            trial["record"]=record
            queues.charged(family,parent)
            if record["status"]=="success" and (best is None or score(record)<score(best)):
                best=record;trial["accepted"]=True;atomic_json(out/"best.plan.json",candidate["plan"])
                # Always record the new observation, but do not displace old queues.
                if refresh_count<1 and last_refresh_span and score(best)[0]<=.99*last_refresh_span:
                    for f in order:
                        if f in ("structure","wcc"):continue
                        queues.add(f,best["record_path"],proposals(f,best,structure,problem,cores,deadline,diagnostics))
                    refresh_count+=1;last_refresh_span=score(best)[0]
            trial["elapsed_seconds"]=time.monotonic()-started
            atomic_json(out/"progress.json",dict(done=False,calls=len(calls),budget=budget,
                best_record=best,elapsed_seconds=time.monotonic()-started))
    elapsed=time.monotonic()-started
    result=dict(case=case,problem=problem,num_cores=cores,variant=variant,method="joint_v3",
        status="success" if best else "no_feasible_result",best_record=best,calls=calls,logical_calls=len(calls),
        new_calls=sum(not c["record"].get("cache_hit",False) for c in calls),budget=budget,
        elapsed_seconds=elapsed,tail_generation_seconds=generation_seconds,
        prefix_summary=str(out/"prefix/summary.json"),prefix_elapsed_seconds=prefix["elapsed_seconds"],
        diagnostics=diagnostics,skipped=skipped,refresh_count=refresh_count,
        stop_reason="call_budget" if len(calls)>=budget else "time_budget" if time.monotonic()>=deadline else "pool_exhausted",
        scope="cold start; strong prefix plus retained tail; all calls and generation share cap/deadline; no historical plans")
    assert len(calls)<=budget
    atomic_json(out/"summary.json",result);return result

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--case",type=int,required=True);p.add_argument("--problem",type=int,required=True)
    p.add_argument("--cores",type=int,default=5);p.add_argument("--out",type=Path,required=True)
    p.add_argument("--budget",type=int,default=16);p.add_argument("--seconds",type=float,default=180)
    p.add_argument("--evaluation-timeout",type=float,default=45);p.add_argument("--variant",choices=("strong","joint"),default="joint")
    a=p.parse_args();s=run(f"case_{a.case:03d}",a.problem,a.cores,a.out,a.budget,a.seconds,a.evaluation_timeout,a.variant)
    print(json.dumps({k:s[k] for k in ("status","logical_calls","elapsed_seconds","stop_reason")}))
