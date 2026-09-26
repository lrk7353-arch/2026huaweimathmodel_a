"""Replay only timed-out fixed plans, preserving the original failed ledger."""
import argparse
import json
from pathlib import Path
import time
from common_run import atomic_json,read_json
from persistent_budget import exact_signature
from run_server_full import run_jobs

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--root',type=Path,required=True)
p.add_argument('--workers',type=int,default=4)
p.add_argument('--timeout',type=int,default=600)
a=p.parse_args()
if not 1<=a.workers<=8 or not 120<a.timeout<=900:p.error('bounded workers and extended timeout required')
root=a.root.resolve()
original=[]; jobs=[]
for path in sorted((root/'replay/jobs').glob('*/result.json')):
    r=read_json(path); original.append(r)
    if r['status']=='success':continue
    record=r.get('best_record') or {}
    if record.get('status')!='timeout':
        raise ValueError('This recovery is only for timeouts; investigate other failures first')
    job=dict(r['job'],evaluation_timeout=a.timeout,original_failed_result=str(path))
    jobs.append(job)
if len(original)!=1500 or len({r['job']['id'] for r in original})!=1500:
    raise ValueError('Incomplete original 1500 replay ledger')
for j in jobs:
    before=read_json(j['original_failed_result'])['best_record']
    if exact_signature(read_json(j['plan']))!=exact_signature(read_json(before['plan_path'])):
        raise ValueError('Plan changed before retry')
deadline=read_json(root/'run_manifest.json')['deadline_epoch']
retries=run_jobs(jobs,root/'replay_timeout_retry',a.workers,deadline)
resolved={r['job']['id']:r for r in retries if r['status']=='success'}
for ident,r in resolved.items():
    before=read_json(r['job']['original_failed_result'])['best_record']
    after=r['best_record']
    for field in ('graph_sha256','config_sha256','official_py_sha256'):
        if before['hashes'][field]!=after['hashes'][field]:raise ValueError('Official input changed')
    if exact_signature(read_json(before['plan_path']))!=exact_signature(read_json(after['plan_path'])):
        raise ValueError('Retry plan encoding changed')
passed=sum(r['status']=='success' or r['job']['id'] in resolved for r in original)
summary=dict(expected=1500,original_success=1500-len(jobs),original_timeouts=len(jobs),
    retries_finished=len(retries),recovered=len(resolved),verified=passed,
    all_verified=passed==1500,original_calls=sum(r.get('calls',0) for r in original),
    additional_calls=sum(r.get('calls',0) for r in retries),
    elapsed_by_job={r['job']['id']:r.get('elapsed_seconds') for r in retries},
    records={key:r['best_record']['record_path'] for key,r in resolved.items()},
    note='Original failed results remain unchanged; only identical fixed plans replayed, no search budget reset')
atomic_json(root/'replay_reconciliation.json',summary)
print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
