"""Mix joint, mature and intact-component candidates for a paid cold parent."""
import gzip
import json
from pathlib import Path
import sys
import time
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parent))
from common_run import DATA,GraphIR,read_json,atomic_json
from wait_candidate_worker import generate as mature_generate
from critical_wait_candidates import generate as joint_generate
from controller import generate_wcc_candidates


if __name__=='__main__':
    req=read_json(sys.argv[1]);t=time.monotonic();ir=GraphIR.from_path(DATA/(req['case']+'.json'))
    try:
        old,old_diag=mature_generate(dict(req,policy='mature'))
        with gzip.open(req['result'],'rt') as f:raw=json.load(f)
        new,new_diag=joint_generate(ir,read_json(req['plan']),raw,req['cores'],24,seconds=max(.001,17-(time.monotonic()-t)))
        whole=[];whole_diag={}
        if req.get('component_plan') and time.monotonic()-t<17:
            whole,whole_diag=generate_wcc_candidates(ir,read_json(req['component_plan']),num_cores=req['cores'],max_candidates=24,seed=17,policy='mixed')
        pools=[new,old,whole]
        cs=[dict(g[i],cold_family=family) for i in range(max(map(len,pools),default=0))
            for family,g in zip(('joint','mature','intact_wcc'),pools) if i<len(g)]
        value=dict(status='success',candidates=cs,diagnostics=dict(joint=new_diag,mature=old_diag,wcc=whole_diag))
    except Exception as exc:
        import traceback
        value=dict(status='generation_error',error=repr(exc),traceback=traceback.format_exc(),candidates=[])
    value['seconds']=time.monotonic()-t;atomic_json(sys.argv[2],value)
