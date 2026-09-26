"""Replay registered major positive/negative cases; no candidate feedback."""
import json
import hashlib
from concurrent.futures import ProcessPoolExecutor,as_completed
from pathlib import Path
import sys
HERE=Path(__file__).resolve().parents[1];ROOT=HERE.parent;sys.path.insert(0,str(ROOT))
from common_run import DATA,read_json,atomic_json,evaluate,score,write_csv
SOURCE=ROOT/'运行结果/持续联合冲刺_20260926/正式75配置_v2_6并发'


def one(job):
    case,source_problem,variant,target_problem=job
    source=read_json(SOURCE/f'configurations/{case}_p{source_problem}_n5/{variant}/attempt_001/summary.json')['best_record']
    out=HERE/'归因复评'/f'{case}_{variant}_p{target_problem}'
    previous=[read_json(p) for p in out.glob('attempts/*/record.json')]
    matching=[r for r in previous if r.get('status')=='success' and r.get('problem')==target_problem
        and all(r['hashes'][k]==source['hashes'][k] for k in
                ('plan_sha256','graph_sha256','config_sha256','official_py_sha256'))]
    reused=bool(matching)
    rec=matching[0] if reused else evaluate(DATA/(case+'.json'),read_json(source['plan_path']),target_problem,
        out,timeout=180,config_path=DATA/'config.txt')
    assert rec['status']=='success'
    assert hashlib.sha256(Path(rec['result_path']).read_bytes()).hexdigest()==rec['result_sha256']
    for k in ('plan_sha256','graph_sha256','config_sha256','official_py_sha256'):
        assert rec['hashes'][k]==source['hashes'][k]
    if target_problem==source_problem:
        # Host process RSS depends on worker history; modeled L1/UB peaks remain checked.
        deterministic=lambda m:{k:v for k,v in m.items() if k!='peak_memory_bytes'}
        assert deterministic(rec['metrics'])==deterministic(source['metrics'])
    return dict(case=case,source_problem=source_problem,variant=variant,problem=target_problem,source=source,record=rec,reused_existing_record=reused)


def main():
    jobs=[(f'case_{c:03d}',1,v,1)for c in (85,47)for v in ('mature','legacy','persistent')]
    jobs += [('case_046',3,v,p)for v in ('mature','legacy','persistent')for p in (2,3)]
    results=[]
    with ProcessPoolExecutor(max_workers=4)as pool:
        for f in as_completed([pool.submit(one,j)for j in jobs]):
            r=f.result();results.append(r);print(r['case'],r['variant'],r['problem'],score(r['record']),flush=True)
    atomic_json(HERE/'重点正负例复评.json',dict(logical_calls=len(results),records=results,
        new_calls_this_assembly=sum(not r['reused_existing_record'] for r in results),
        audit_correction='Initial assembly compared host peak_memory_bytes (RSS) and stopped. All 12 official runs had succeeded. Reassembly reuses their hashed results and compares all deterministic metrics, including modeled L1/UB memory peaks.',
        purpose='Fixed-plan reproduction and same-plan P2/P3 explanation. No search, no feedback into the frozen comparison.'))
    rows=[]
    for r in results:
        m=r['record']['metrics'];d=m['data_movement_bytes'];cache=m.get('cache_stats',{})or{}
        rows.append(dict(case=r['case'],variant=r['variant'],problem=r['problem'],makespan=m['makespan'],
            copy=d['added_copy_bytes'],spill=d['spill_added_copy_bytes'],hit_bytes=cache.get('hit_bytes'),miss_bytes=cache.get('miss_bytes'),hit_rate=cache.get('hit_rate')))
    write_csv(HERE/'重点正负例复评.csv',sorted(rows,key=lambda r:(r['case'],r['variant'],r['problem'])))


if __name__=='__main__':main()
