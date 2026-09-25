"""Isolated candidate generation, bounded by its caller's wall-clock timeout."""
import gzip
import json
import sys
import time
from common_run import DATA,GraphIR,read_json,atomic_json
from refine_regions import candidates


def generate(request):
    ir=GraphIR.from_path(DATA/(request['case']+'.json'));plan=read_json(request['plan'])
    with gzip.open(request['result'],'rt') as f:raw=json.load(f)
    p=request['problem'];n=request['cores'];r=request['round'];limit=request['limit']
    if request['policy']!='mature':return candidates(ir,plan,raw,p,n,request['policy'],r,limit)
    if p==1:
        from joint_solver import critical_candidates
        details=[];first=critical_candidates(ir,plan,raw,n,time.monotonic()+8,details)
        second,d=candidates(ir,plan,raw,p,n,'joint',r,limit)
        pools=[first,second];diag=dict(critical=details,joint=d)
    else:
        pools=[];diag={}
        for family in (['data','region','legacy'] if p==2 else ['legacy','data','reads']):
            cs,d=candidates(ir,plan,raw,p,n,family,r,limit);pools.append(cs);diag[family]=d
    result=[g[i] for i in range(max(map(len,pools),default=0)) for g in pools if i<len(g)]
    return result[:limit],diag


if __name__=='__main__':
    request=read_json(sys.argv[1]);started=time.monotonic()
    try:
        cs,diag=generate(request);value=dict(status='success',candidates=cs,diagnostics=diag)
    except Exception as exc:
        import traceback
        value=dict(status='generation_error',error=repr(exc),traceback=traceback.format_exc(),candidates=[])
    value['generation_seconds']=time.monotonic()-started;atomic_json(sys.argv[2],value)
