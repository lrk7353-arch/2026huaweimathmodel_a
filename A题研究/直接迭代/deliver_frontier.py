"""Independently replay selected improvements and export a portable 1500 ledger."""
import argparse
import concurrent.futures
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil

from common_run import DATA, atomic_json, evaluate, read_json, score

HERE=Path(__file__).resolve().parent
LEDGER=HERE/'三指标联合推进_20260926/累计1500配置成绩.csv'


def verify(item):
    key,source,out=item
    plan=read_json(source['plan_path'])
    record=evaluate(DATA/(key[0]+'.json'),plan,key[1],Path(out)/'evaluations',timeout=120,config_path=DATA/'config.txt')
    if record['status']!='success':raise ValueError((key,record['status'],record.get('error')))
    for field in ('plan_sha256','graph_sha256','config_sha256','official_py_sha256'):
        if record['hashes'][field]!=source['hashes'][field]:raise ValueError((key,'hash mismatch',field))
    if score(record)!=score(source):raise ValueError((key,'score mismatch'))
    for field in ('data_movement_bytes','cache_stats'):
        if record['metrics'].get(field)!=source['metrics'].get(field):raise ValueError((key,'metric mismatch',field))
    path=Path(out)/'精选方案'/f'p{key[1]}'/f'n{key[2]}'/(key[0]+'_multicore_res.json')
    atomic_json(path,plan)
    return key,source,record,str(path)


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--roots',nargs='+',required=True);p.add_argument('--workers',type=int,default=4)
    p.add_argument('--ledger',type=Path,default=LEDGER)
    p.add_argument('--source-commit',default='2da035b')
    p.add_argument('--include-cold',action='store_true')
    args=p.parse_args();out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    ledger=args.ledger.resolve()
    rows=list(csv.DictReader(ledger.open(encoding='utf-8-sig')))
    baseline={(r['case'],int(r['problem']),int(r['cores'])):r for r in rows}
    selected={};calls=[];summaries=[];seen_summaries=set()
    for directory in args.roots:
        root=Path(directory)
        if (root/'results.json').exists():
            indexed=read_json(root/'results.json')
            files=[Path(r['summary']) for r in indexed if isinstance(r,dict) and r.get('summary')]
        else:files=[]
        if not files:files=sorted(root.rglob('summary.json'))
        for file in files:
            file=file.resolve()
            if file in seen_summaries:continue
            seen_summaries.add(file)
            s=read_json(file)
            if not s.get('complete') or not all(k in s for k in ('case','problem','cores','calls','best_record')):continue
            if not args.include_cold and 'ledger_baseline' not in s:continue
            key=(s['case'],s['problem'],s['cores']);best=s.get('best_record')
            for call in s['calls']:calls.append(dict(case=key[0],problem=key[1],cores=key[2],batch=directory,**call))
            summaries.append(dict(path=str(file),generation_seconds=s['generation_seconds'],
                elapsed_seconds=s['elapsed_seconds'],generation_errors=s['generation_errors'],calls=len(s['calls'])))
            if best and score(best)<(int(baseline[key]['makespan']),int(baseline[key]['added_copy'])):
                if key not in selected or score(best)<score(selected[key]):selected[key]=best
    atomic_json(out/'接收清单.json',dict(input_ledger_sha256=hashlib.sha256(ledger.read_bytes()).hexdigest(),
        selected=[dict(case=k[0],problem=k[1],cores=k[2],source=v) for k,v in sorted(selected.items())]))
    verified={}
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        jobs=[pool.submit(verify,(k,v,str(out))) for k,v in sorted(selected.items())]
        for job in concurrent.futures.as_completed(jobs):
            k,source,record,path=job.result();verified[k]=dict(source=source,record=record,path=path)
            atomic_json(out/'复评进度.json',dict(complete=False,verified=len(verified),total=len(selected)))
    for row in rows:
        key=(row['case'],int(row['problem']),int(row['cores']))
        if key in verified:
            v=verified[key];record=v['record']
            row.update(makespan=score(record)[0],added_copy=score(record)[1],
                speedup=float(row['original_singlecore'])/score(record)[0],
                source='hardware_frontier',source_commit=args.source_commit,source_plan=v['source']['plan_path'],
                plan=os.path.relpath(v['path'],out),plan_sha256=record['hashes']['plan_sha256'],
                verification='independent_official_time_copy_cache_and_input_hash_match')
        else:
            row['plan']=os.path.relpath((ledger.parent/row['plan']).resolve(),out)
        if not (out/row['plan']).is_file():raise FileNotFoundError(row['plan'])
    with (out/'累计1500配置成绩.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=rows[0]);w.writeheader();w.writerows(rows)
    with gzip.open(out/'开发及全图全部调用.json.gz','wt',encoding='utf-8') as f:
        json.dump(dict(calls=calls,batches=summaries),f,ensure_ascii=False,separators=(',',':'))
    atomic_json(out/'独立复评.json',dict(complete=True,verified=[dict(case=k[0],problem=k[1],cores=k[2],**v)
        for k,v in sorted(verified.items())]))
    means={p:sum(float(r['speedup']) for r in rows if int(r['problem'])==p and int(r['cores'])==5)/100 for p in (1,2,3)}
    result=dict(complete=True,improved_configurations=len(verified),five_core_mean_speedups=means,
        exploratory_official_calls=len(calls),independent_official_calls=len(verified),
        scope='cumulative selected library; not cold solver scores; unchanged entries inherited with relative paths')
    atomic_json(out/'交付摘要.json',result);atomic_json(out/'复评进度.json',dict(complete=True,verified=len(verified),total=len(selected)))
    print(json.dumps(result),flush=True)


if __name__=='__main__':main()
