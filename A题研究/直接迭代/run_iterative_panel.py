"""Execute the frozen paired A/B panel with one factor and isolated fresh caches."""
import argparse
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import subprocess
from common_run import *
from p1_iterative_tasks import run

ROOT = R.parent


def extension_signal(rows, trial_lists):
    capped = [x for x in rows if x['method']=='iterative'
              and x['status']=='success' and x['stop_reason']=='call_budget']
    late = [x for x in capped if any(t['accepted'] and t['generation'] >= 2
            for t in trial_lists[x['case'],x['cores']][-2:])]
    return len(capped) >= 2 and bool(late)


def one(job, protocol, root):
    case, n = job['case'], job['cores']
    out = Path(root)/case/f'n{n}'
    started = time.monotonic()
    old = run_candidate(case, 1, n, read_json(ROOT/job['plan']), out/'baseline',
                        timeout=min(protocol['evaluation_timeout_seconds'], protocol['total_seconds']))
    baseline_seconds = time.monotonic()-started
    rows = []
    if old['status'] != 'success' or score(old)[0] != job['expected_baseline']:
        status = 'baseline_failed' if old['status'] != 'success' else 'baseline_drift'
        for method in job['method_order']:
            rows.append(dict(case=case, cores=n, method=method, status=status,
                before=job['expected_baseline'], after=None, logical_calls=1,
                candidate_new_calls=0, baseline_new_calls=int(not old['cache_hit']),
                errors=1, timeouts=int(old['status']=='timeout'),
                elapsed_seconds=baseline_seconds, stop_reason=status))
        write_csv(out/'results.csv', rows)
        return rows
    remaining = protocol['total_seconds']-baseline_seconds
    for method in job['method_order']:
        if remaining <= 0:
            raise RuntimeError('baseline exhausted total time budget')
        s = run(case, old, out/method, protocol['call_budget']-1, remaining,
                cores=n, seed=protocol['seed'], refresh_after_accept=method=='iterative')
        row = dict(case=case, cores=n, method=method, status='success',
            before=s['before'], after=s['after'],
            after_copy_bytes=score(s['best_record'])[1], original_singlecore=job['original_singlecore'],
            logical_calls=1+s['logical_calls'], candidate_new_calls=s['new_calls'],
            baseline_new_calls=int(not old['cache_hit']),
            errors=sum(t['record']['status'] not in ('success','timeout') for t in s['evaluations']),
            timeouts=sum(t['record']['status']=='timeout' for t in s['evaluations']),
            elapsed_seconds=baseline_seconds+s['elapsed_seconds'],
            generation_count=len(s['generations']), stop_reason=s['stop_reason'],
            summary=str(out/method/'summary.json'))
        rows.append(row)
        write_csv(out/'results.csv', rows)
        print(json.dumps(row,ensure_ascii=False), flush=True)
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--protocol',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--budget',type=int,choices=(8,12),default=8)
    p.add_argument('--extension-of',type=Path,help='Completed budget-8 output; required for the predeclared budget-12 extension')
    a = p.parse_args()
    protocol = read_json(a.protocol)
    extension = None
    if a.budget == 12:
        if not a.extension_of:
            p.error('budget 12 requires --extension-of')
        previous = read_json(a.extension_of/'summary.json')
        prior_execution = read_json(a.extension_of/'execution.json')
        if not previous['completed'] or prior_execution['protocol_sha256'] != hashlib.sha256(a.protocol.read_bytes()).hexdigest():
            p.error('extension requires a completed run of this exact frozen protocol')
        trials = {(x['case'],x['cores']):read_json(Path(x['summary']))['evaluations']
                  for x in previous['rows'] if x['method']=='iterative'}
        if not extension_signal(previous['rows'],trials):
            p.error('predeclared extension condition was not met')
        extension = dict(previous=str(a.extension_of), previous_summary_sha256=hashlib.sha256((a.extension_of/'summary.json').read_bytes()).hexdigest())
        protocol['call_budget'] = 12
    elif a.extension_of:
        p.error('--extension-of requires budget 12')
    if protocol['evaluation_timeout_seconds'] != 60:
        p.error('controller currently uses a fixed 60-second per-call limit')
    for rel,digest in {**protocol['python_sha256'],**protocol['official_sha256']}.items():
        if hashlib.sha256((ROOT/rel).read_bytes()).hexdigest() != digest:
            p.error('frozen file changed: '+rel)
    for job in protocol['jobs']:
        if hashlib.sha256((ROOT/job['plan']).read_bytes()).hexdigest() != job['plan_sha256']:
            p.error('frozen incumbent changed')
    out = a.out.resolve()
    if out == DATA or DATA in out.parents:
        p.error('outputs must be outside official data')
    out.mkdir(parents=True,exist_ok=False)
    atomic_json(out/'protocol.json',protocol)
    atomic_json(out/'execution.json',dict(command=sys.argv,
        protocol_sha256=hashlib.sha256(a.protocol.read_bytes()).hexdigest(),
        extension=extension,
        git_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()))
    start = time.monotonic()
    rows, failures = [], []
    pending = iter(protocol['jobs'])
    with ProcessPoolExecutor(max_workers=protocol['workers']) as pool:
        active = {pool.submit(one, j, protocol, str(out)):j
                  for _,j in zip(range(protocol['workers']),pending)}
        stop_dispatch = False
        while active:
            done,_ = wait(active,return_when=FIRST_COMPLETED)
            for future in done:
                job = active.pop(future)
                try:
                    result = future.result()
                    rows.extend(result)
                    if any(x['status'] != 'success' for x in result):
                        stop_dispatch = True
                except Exception as exc:
                    failures.append(dict(job=job,error=repr(exc)))
                    stop_dispatch = True
                write_csv(out/'results.csv',rows)
                atomic_json(out/'progress.json',dict(completed=len(rows),expected=2*len(protocol['jobs']),
                    failures=failures,stop_dispatch=stop_dispatch,elapsed_seconds=time.monotonic()-start))
                if not stop_dispatch:
                    next_job = next(pending,None)
                    if next_job is not None:
                        active[pool.submit(one,next_job,protocol,str(out))] = next_job
    result = dict(completed=len(rows)==2*len(protocol['jobs']) and not failures,
        rows=rows,failures=failures,elapsed_seconds=time.monotonic()-start,
        logical_calls=sum(x['logical_calls'] for x in rows),
        actual_new_calls=sum(not read_json(p)['cache_hit'] for p in out.rglob('record.json')))
    atomic_json(out/'summary.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
