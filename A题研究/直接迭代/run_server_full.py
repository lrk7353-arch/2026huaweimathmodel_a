"""Isolated subprocess jobs, bounded concurrency and resumable full evaluation.

No historical plans enter cold search. Each started job has a private attempt;
an interrupted attempt is retained, never silently restarted on resume.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import csv
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

from common_run import DATA, GraphIR, atomic_json, read_json, validate_plan, evaluate, score, write_csv
from accept_p1_relay_gains import deterministic_metrics
from persistent_budget import exact_signature


SEARCH_PROTOCOL = 'incumbent_first_v1'


def pause_requested(root):
    root=Path(root)
    return (root/'pause.request').exists() or (root.parent/'user_pause.json').exists()


def recover_paid_incumbent(job, directory, previous):
    """Keep a verified current-job answer even if the optimizer exits abnormally."""
    if job['kind'] != 'cold' or previous.get('status') == 'success':
        return previous
    directory=Path(directory).resolve()
    attempts=directory/'search/evaluations/attempts'
    records=[]
    attempt_dirs=list(attempts.glob('*'))
    for attempt in attempt_dirs:
        path=attempt/'record.json'
        if not path.exists():continue
        try:
            record=read_json(path)
            request=read_json(attempt/'request.json')
        except (OSError,ValueError):continue
        if record.get('status')!='success':continue
        if request.get('hashes')!=record.get('hashes'):continue
        if (record.get('problem')!=job['problem'] or
            record.get('metrics',{}).get('num_cores')!=job['cores'] or
            Path(record.get('graph_path','')).resolve()!=(DATA/(job['case']+'.json')).resolve()):
            continue
        if Path(record.get('record_path','')).resolve()!=path.resolve():continue
        plan_path=Path(record.get('plan_path','')).resolve()
        result_path=Path(record.get('result_path','')).resolve()
        if (not plan_path.is_relative_to(attempt.resolve()) or not plan_path.is_file()
                or not result_path.is_relative_to(attempt.resolve()) or not result_path.is_file()):continue
        if (request.get('problem')!=job['problem'] or
                Path(request.get('plan_path','')).resolve()!=plan_path or
                Path(request.get('graph_path','')).resolve()!=Path(record['graph_path']).resolve()):continue
        def digest(path):
            with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
        if digest(result_path)!=record.get('result_sha256'):continue
        if digest(plan_path)!=record.get('hashes',{}).get('plan_sha256'):continue
        try:
            plan=read_json(plan_path)
            ir=GraphIR.from_path(DATA/(job['case']+'.json'))
            validate_plan(ir,plan)
        except (OSError,ValueError,KeyError):continue
        if len(plan['core_schedules'])!=job['cores']:continue
        records.append(record)
    if not records:return previous
    best=min(records,key=score)
    # Retain both the charged failures and the abnormal termination evidence.
    return dict(previous,status='success',best_record=best,
        calls=max(previous.get('calls',0),len(attempt_dirs)),
        optimizer_termination=previous.get('status'),
        stop_reason='optimizer_stopped_returning_verified_incumbent',
        recovered_paid_incumbent=True)


def execute(job, directory):
    directory = Path(directory)
    started = time.monotonic()
    if job['kind'] == 'cold':
        from cold_portfolio import run
        s = run(job['case'], 1, job['cores'], job['method'], directory/'search',
                budget=24, seconds=240, evaluation_timeout=60)
        calls = s['calls']
        best = s.get('best_record')
        assert len(calls) == s['logical_calls'] and len(calls) <= 24
        if best:
            assert best['record_path'] in {c['record']['record_path'] for c in calls}
            ir = GraphIR.from_path(DATA/(job['case']+'.json'))
            plan = read_json(best['plan_path'])
            validate_plan(ir, plan)
            assert len(plan['core_schedules']) == job['cores']
        result = dict(status='success' if best else 'no_valid_plan', calls=len(calls),
            failed_calls=sum(c['record']['status'] != 'success' for c in calls),
            generation_errors=sum(x.get('phase') == 'generation_error' for x in s['stages']),
            best_record=best, stop_reason=s['stop_reason'])
    else:
        evaluation_timeout=job.get('evaluation_timeout',120)
        if not isinstance(evaluation_timeout,(int,float)) or not 0 < evaluation_timeout <= 900:
            raise ValueError('Replay timeout must be positive and no more than 900 seconds')
        plan = read_json(job['plan'])
        ir = GraphIR.from_path(DATA/(job['case']+'.json'))
        validate_plan(ir, plan)
        assert len(plan['core_schedules']) == job['cores']
        record = evaluate(ir.path, plan, job['problem'], directory/'evaluation',
                          timeout=evaluation_timeout, config_path=DATA/'config.txt')
        matched = record['status'] == 'success'
        if matched:
            matched = list(score(record)) == job['expected_score']
            if job.get('expected_metrics'):
                matched = matched and deterministic_metrics(record) == job['expected_metrics']
        result = dict(status='success' if matched else 'mismatch_or_failure',
                      calls=1, failed_calls=int(record['status'] != 'success'),
                      matched=matched, best_record=record)
    result.update(job=job, elapsed_seconds=time.monotonic()-started)
    atomic_json(directory/'result.json', result)
    return result


def supervise(job, root):
    directory = Path(root)/'jobs'/job['id']
    directory.mkdir(parents=True, exist_ok=False)
    atomic_json(directory/'job.json', job)
    env = dict(os.environ, OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
               MKL_NUM_THREADS='1', NUMEXPR_NUM_THREADS='1', PYTHONHASHSEED='17')
    with (directory/'stdout.log').open('w') as out, (directory/'stderr.log').open('w') as err:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                 '--job-file', str(directory/'job.json')],
                                stdout=out, stderr=err, env=env, start_new_session=True)
        atomic_json(directory/'process.json', dict(pid=proc.pid, started=time.time()))
        try:
            proc.wait(timeout=360 if job['kind'] == 'cold' else job.get('evaluation_timeout',120)+30)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    result = directory/'result.json'
    if result.exists():
        previous=read_json(result)
        recovered=recover_paid_incumbent(job,directory,previous)
        if recovered is not previous:
            atomic_json(directory/'optimizer_failure.json',previous)
            atomic_json(result,recovered)
        return recovered
    records = list(directory.rglob('record.json'))
    requests = list(directory.rglob('request.json'))
    value = dict(job=job, status='interrupted_or_hard_timeout',
                 calls=max(len(records), len(requests)), failed_calls=None,
                 returncode=proc.returncode)
    previous=value
    value=recover_paid_incumbent(job,directory,value)
    if value is not previous:atomic_json(directory/'optimizer_failure.json',previous)
    atomic_json(result, value)
    return value


def run_jobs(jobs, root, workers, deadline):
    if len({job['id'] for job in jobs}) != len(jobs):
        raise ValueError('Duplicate job IDs')
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root/'batch.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        spec = root/'jobs.json'
        if spec.exists():
            if sorted(read_json(spec),key=lambda j:j['id']) != sorted(jobs,key=lambda j:j['id']):
                raise ValueError('Resume job specification differs; use a new directory')
        else:
            atomic_json(spec, jobs)
        results, todo = [], []
        for job in jobs:
            directory = root/'jobs'/job['id']
            if (directory/'result.json').exists():
                results.append(read_json(directory/'result.json'))
            elif directory.exists():
                if (directory/'process.json').exists():
                    pid=read_json(directory/'process.json')['pid']
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        raise RuntimeError(f'Prior job process {pid} still exists; do not overlap resumes')
                # Preserve any charged incomplete attempt. No free budget reset.
                requests = list(directory.rglob('request.json'))
                results.append(dict(job=job, status='interrupted_previous_run', calls=len(requests)))
            else:
                todo.append(job)
        started = time.monotonic()
        active = {}
        def save():
            rows = []
            for result in results:
                job = result['job']; r = result.get('best_record')
                ok = r and r.get('status') == 'success'
                rows.append(dict(id=job['id'], kind=job['kind'], case=job['case'],
                    problem=job['problem'], cores=job['cores'], method=job.get('method', ''),
                    status=result['status'], makespan=score(r)[0] if ok else None,
                    added_copy=score(r)[1] if ok else None, calls=result.get('calls',0),
                    failed_calls=result.get('failed_calls'),
                    generation_errors=result.get('generation_errors',0),
                    seconds=result.get('elapsed_seconds')))
            complete = len(results) == len(jobs)
            atomic_json(root/'progress.json', dict(complete=complete, expected=len(jobs),
                completed=len(results), success=sum(r['status']=='success' for r in results),
                attention=sum(r['status']!='success' for r in results),
                active=[j['id'] for j in active.values()], remaining=len(jobs)-len(results),
                paused=pause_requested(root),
                recorded_calls=sum(r.get('calls',0) for r in results), workers=workers,
                updated=datetime.now().isoformat(), session_seconds=time.monotonic()-started))
            write_csv(root/'results.csv', rows)
            if complete:
                atomic_json(root/'summary.json', dict(complete=True, rows=rows,
                    success=all(r['status']=='success' for r in results)))
        pending = iter(todo)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            exhausted = False
            while active or not exhausted:
                while not exhausted and len(active) < workers:
                    # The independent replay repair may use a longer per-job cap.
                    required=max([360]+[j.get('evaluation_timeout',120)+30 for j in jobs])
                    if pause_requested(root) or time.time()+required >= deadline:
                        exhausted = True
                        break
                    job = next(pending, None)
                    if job is None:
                        exhausted = True
                        break
                    active[pool.submit(supervise, job, root)] = job
                save()
                if not active:
                    break
                done, _ = wait(active, timeout=10, return_when=FIRST_COMPLETED)
                for future in done:
                    job = active.pop(future)
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append(dict(job=job, status='supervisor_error', error=repr(exc), calls=0))
            save()
        return results


def summarize_cold(results, verified, root, delivery):
    by_key={(r['job']['case'],r['job']['cores'],r['job']['method']):r for r in results}
    checks={r['job']['id']:r['status']=='success' for r in verified}
    with (delivery/'累计1500配置成绩.csv').open(encoding='utf-8-sig',newline='') as f:
        baseline={(r['case'],int(r['cores'])):r for r in csv.DictReader(f) if int(r['problem'])==1}
    rows=[]
    for case,core in sorted({(k[0],k[1]) for k in by_key}):
        a,b=(by_key.get((case,core,m)) for m in ('integrated','joint_tonight'))
        valid=all(r and r['status']=='success' and checks.get(r['job']['id'],False) for r in (a,b))
        row=dict(case=case,cores=core,verified_pair=valid)
        if valid:
            x,y=score(a['best_record']),score(b['best_record'])
            row.update(integrated_makespan=x[0],joint_makespan=y[0],
                integrated_copy=x[1],joint_copy=y[1],time_reduction_percent=100*(1-y[0]/x[0]),
                time_outcome='win' if y[0]<x[0] else 'loss' if y[0]>x[0] else 'tie',
                original_singlecore=int(baseline[case,core]['original_singlecore']))
        rows.append(row)
    write_csv(root/'paired_results.csv',rows)
    groups={}
    for core in sorted({r['cores'] for r in rows}):
        good=[r for r in rows if r['cores']==core and r['verified_pair']]
        groups[str(core)]=dict(verified_pairs=len(good),
            wins=sum(r['time_outcome']=='win' for r in good),
            ties=sum(r['time_outcome']=='tie' for r in good),
            losses=sum(r['time_outcome']=='loss' for r in good),
            integrated_mean_speedup=sum(r['original_singlecore']/r['integrated_makespan'] for r in good)/len(good) if good else None,
            joint_mean_speedup=sum(r['original_singlecore']/r['joint_makespan'] for r in good)/len(good) if good else None)
    atomic_json(root/'comparison_summary.json',dict(pairs=len(rows),
        all_pairs_verified=all(r['verified_pair'] for r in rows),by_cores=groups,
        scope='Same-server cold comparison; speedup denominator inherited from source table; not official total score'))


def apply_replay_recovery(results, root):
    """Use independently validated retry records; leave the failed ledger intact."""
    proof=Path(root)/'replay_reconciliation.json'
    if not proof.exists():return results
    recoveries=read_json(proof).get('records',{})
    combined=[]
    for result in results:
        ident=result['job']['id']
        if result['status']=='success' or ident not in recoveries:
            combined.append(result);continue
        before=result.get('best_record') or {}
        after=read_json(recoveries[ident]);job=result['job']
        if before.get('status')!='timeout' or after.get('status')!='success':
            raise ValueError('Only successful timeout replays can resolve the gate')
        if after['problem']!=job['problem'] or after['metrics']['num_cores']!=job['cores']:
            raise ValueError('Recovery scene/core mismatch')
        if list(score(after))!=job['expected_score']:
            raise ValueError('Recovery score mismatch')
        for field in ('graph_sha256','config_sha256','official_py_sha256'):
            if before['hashes'][field]!=after['hashes'][field]:
                raise ValueError('Recovery official input mismatch')
        if exact_signature(read_json(before['plan_path']))!=exact_signature(read_json(after['plan_path'])):
            raise ValueError('Recovery plan changed')
        combined.append(dict(result,status='success',best_record=after,
            calls=result.get('calls',0)+1,recovered_from_timeout=True))
    return combined


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-file', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--delivery', type=Path)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--stage', choices=('replay','cold','all'), default='all')
    parser.add_argument('--cases', default=','.join(str(n) for n in range(1,101)))
    parser.add_argument('--cores', default='1,2,3,4,5')
    parser.add_argument('--problems', default='1,2,3')
    parser.add_argument('--hours', type=float, default=4)
    args = parser.parse_args()
    if args.job_file:
        try:
            execute(read_json(args.job_file), args.job_file.parent)
        except Exception as exc:
            atomic_json(args.job_file.parent/'result.json', dict(job=read_json(args.job_file),
                status='exception', error=repr(exc), traceback=traceback.format_exc(),
                calls=max(len(list(args.job_file.parent.rglob('record.json'))),
                          len(list(args.job_file.parent.rglob('request.json'))))))
            raise
        return
    if not 1 <= args.workers <= 12 or not args.out or not args.delivery or not 0 < args.hours <= 12:
        parser.error('Need out, delivery, workers 1..12 and bounded hours')
    cases = sorted(set(int(n) for n in args.cases.split(',')))
    cores = sorted(set(int(n) for n in args.cores.split(',')))
    problems = sorted(set(int(n) for n in args.problems.split(',')))
    if not set(cases)<=set(range(1,101)) or not set(cores)<=set(range(1,6)) or not set(problems)<={1,2,3}:
        parser.error('invalid case/core/problem selection')
    root=args.out.resolve(); delivery=args.delivery.resolve()
    root.mkdir(parents=True,exist_ok=True)
    if (root/'user_pause.json').exists():
        parser.error('This batch is explicitly paused; do not resume it implicitly')
    protocol=dict(search_protocol=SEARCH_PROTOCOL,workers=args.workers,stage=args.stage,cases=cases,cores=cores,
                  problems=problems,delivery=str(delivery),call_budget=24,route_seconds=240)
    manifest=root/'run_manifest.json'
    if manifest.exists():
        saved=read_json(manifest)
        if saved['protocol']!=protocol:
            raise ValueError('Run protocol changed; use a new output directory')
        deadline=saved['deadline_epoch']
    else:
        deadline=time.time()+3600*args.hours
        atomic_json(manifest,dict(protocol=protocol,deadline_epoch=deadline,
            started=datetime.now().isoformat(),python=sys.version))
    if args.stage in ('replay','all'):
        with (delivery/'累计1500配置成绩.csv').open(encoding='utf-8-sig', newline='') as f:
            rows=list(csv.DictReader(f))
        jobs=[dict(id=f"{r['case']}_p{r['problem']}_n{r['cores']}",kind='replay',
            case=r['case'],problem=int(r['problem']),cores=int(r['cores']),
            plan=str(delivery/r['plan']),expected_score=[int(r['makespan']),int(r['added_copy'])])
            for r in rows if int(r['case'][-3:]) in cases and int(r['cores']) in cores and int(r['problem']) in problems]
        results=run_jobs(jobs,root/'replay',args.workers,deadline)
        results=apply_replay_recovery(results,root)
        if len(results)!=len(jobs) or any(r['status']!='success' for r in results):
            atomic_json(root/'attention.json',dict(reason='Replay incomplete or mismatched; cold phase not started'))
            return
        if (root/'attention.json').exists():
            atomic_json(root/'attention.json',dict(resolved=True,
                reason='All 1500 plans verified; timeout attempts retained separately'))
    if args.stage in ('cold','all'):
        jobs=[]
        # Rotate method order while retaining adjacent paired configurations.
        for case in cases:
            for core in cores:
                methods=['integrated','joint_tonight']
                if (case+core)%2:methods.reverse()
                for method in methods:
                    jobs.append(dict(id=f'case_{case:03}_p1_n{core}_{method}',kind='cold',
                        case=f'case_{case:03}',problem=1,cores=core,method=method))
        results=run_jobs(jobs,root/'cold',args.workers,deadline)
        if len(results)!=len(jobs):
            atomic_json(root/'attention.json',dict(reason='Cold phase incomplete at deadline; verification deferred'))
            return
        verify=[]
        for r in results:
            record=r.get('best_record')
            if record and record.get('status')=='success':
                job=r['job']
                # Independently replay both routes' final answers, including losers.
                verify.append(dict(id=job['id'],kind='verify',case=job['case'],problem=1,
                    cores=job['cores'],method=job['method'],plan=record['plan_path'],
                    expected_score=list(score(record)),expected_metrics=deterministic_metrics(record)))
        verified=run_jobs(verify,root/'cold_verification',args.workers,deadline)
        summarize_cold(results,verified,root,delivery)


if __name__ == '__main__':
    main()
