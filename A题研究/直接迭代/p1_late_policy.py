"""Spend the final P1 calls only after the paid incumbent has stagnated."""
import gzip
import json
import time

from common_run import score,read_json
from barrier_bands import candidates as bands
from critical_contract import candidates as contracts,chain


def admit(calls,window=6):
    best=None;last=0
    for i,call in enumerate(calls,1):
        r=call['record']
        if r['status']=='success' and (best is None or score(r)<best):
            best=score(r);last=i
    return dict(admitted=best is not None and len(calls)-last>=window,
                last_improvement_call=last,observed_calls=len(calls),window=window,
                criterion='no lexicographic makespan/COPY improvement in the last window of paid calls')


def proposals(ir,best,cores,deadline):
    with gzip.open(best['result_path'],'rt') as f:raw=json.load(f)
    path,_=chain(raw);wait=sum(w for _,w in path)
    ratio=wait/max(1,raw['makespan']);parent=read_json(best['plan_path'])
    pools={};errors=[];start=time.monotonic()
    order=['contract','bands'] if ratio>=.2 else ['bands','contract']
    for family in order:
        pools[family]=[]
        try:
            stream=(contracts(ir,1,cores,parent,raw,deadline) if family=='contract'
                    else bands(ir,1,cores,parent,deadline))
            for c in stream:pools[family].append(c)
        except (TimeoutError,ValueError) as exc:errors.append(dict(family=family,error=repr(exc)))
        pools[family].sort(key=lambda c:c['metadata']['task_proxy'])
    # Keep one chance for both mechanisms; subsequent slots favor the diagnosed
    # family. This is ranking, not a claim that boundary time is all removable.
    first,second=(pools[x] for x in order)
    queue=first[:2]+second[:1]+first[2:]+second[1:]
    return queue,dict(primary_family=order[0],observed_boundary_fraction=ratio,
        observed_boundary_cycles=wait,generation_seconds=time.monotonic()-start,
        generated={k:len(v) for k,v in pools.items()},errors=errors,
        scope='paid Task trajectory; no official graph recompilation; estimates only rank complete candidates')
