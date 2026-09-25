"""Resumable per-slot unified runs; every slot owns its evaluation processes.

The manifest pins inputs, source hashes and search settings. A completed slot is
reused only under that exact manifest. An incomplete slot is never overwritten.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

from common_run import DATA, R, atomic_json, read_json, write_csv, evaluate
from unified_solver import solve_unified


def worker(job):
    case, p, n, variant, out, budget, seconds, timeout, seed, stage, experience, incumbent = job
    summary = Path(out) / 'summary.json'
    if summary.exists():
        return read_json(summary)
    if p == 0:
        started = time.monotonic()
        Path(out).mkdir(parents=True, exist_ok=False)
        record = evaluate(DATA/(case+'.json'), None, 0, Path(out)/'evaluations',
                          timeout=seconds, config_path=DATA/'config.txt')
        ok = record['status'] == 'success'
        result = dict(case=case, problem=0, num_cores=1, variant='official_singlecore',
            best_record=record if ok else None, best=dict(name='official_singlecore', record=record) if ok else None,
            returned_valid=ok, logical_calls=1, new_calls=int(not record['cache_hit']),
            evaluations=[dict(name='official_singlecore', metadata={}, record=record, accepted=ok, generation_seconds=0)],
            generation_seconds=0, elapsed_seconds=time.monotonic()-started, stop_reason='singlecore_baseline',
            graph_sha256=record['hashes'].get('graph_sha256'))
        atomic_json(summary, result)
        return result
    return solve_unified(DATA / (case + '.json'), p, n, out, call_budget=budget,
        seconds=seconds, evaluation_timeout=timeout, seed=seed, variant=variant,
        stage=stage, experience_path=experience, incumbent=incumbent)


def summarize(s):
    rec = s['best_record']
    old = [x['record']['metrics']['makespan'] for x in s['evaluations']
           if x.get('source', 'old') == 'old' and x['record']['status'] == 'success']
    return dict(case=s['case'], problem=s['problem'], cores=s['num_cores'], variant=s['variant'],
        valid=s['returned_valid'], makespan=rec['metrics']['makespan'] if rec else None,
        best_name=s['best']['name'] if s['best'] else None, best_seed=min(old) if old else None,
        improvement_vs_evaluated_seeds=1 - rec['metrics']['makespan'] / min(old) if old and rec else None,
        calls=s['logical_calls'], new_calls=s['new_calls'], seconds=s['elapsed_seconds'],
        generation_seconds=s['generation_seconds'], new_strategy_calls=sum(x['metadata'].get('new_strategy', False) for x in s['evaluations']),
        failures=sum(x['record']['status'] != 'success' for x in s['evaluations']),
        timeouts=sum(x['record']['status'] == 'timeout' for x in s['evaluations']),
        peak_worker_rss=max((x['record'].get('peak_memory_bytes') or 0 for x in s['evaluations']), default=0),
        stop_reason=s['stop_reason'], best_record_path=rec.get('record_path') if rec else None)


def run(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    cases = [f'case_{i:03d}' for i in args.cases]
    incumbents = {}
    if args.incumbent_ledger:
        with args.incumbent_ledger.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                key = row['case'], int(row['problem']), int(row['cores'])
                if key[0] in cases and key[1] in args.problems and key[2] in args.cores:
                    path = Path(row['plan'])
                    if not path.is_absolute():
                        path = (args.incumbent_root or R.parent)/path
                    assert path.is_file(), path
                    incumbents[key] = str(path.resolve())
        assert len(incumbents) == len(cases)*len(args.problems)*len(args.cores)
    source_files = [p for folder in (R / 'solver', R / 'advanced_solver', R / '精修求解器', R / '探索', Path(__file__).parent)
                    for p in folder.glob('*.py')]
    manifest = dict(cases=cases, problems=args.problems, cores=args.cores, variants=args.variants,
        budget=args.budget, seconds=args.seconds, timeout=args.timeout, seed=args.seed, workers=args.workers,
        stage=args.stage, experience_sha256=hashlib.sha256(args.experience.read_bytes()).hexdigest() if args.experience else None,
        adaptive_workers=args.adaptive_workers, heavy_slots=args.heavy_slots,
        concurrency_policy=args.concurrency_policy,
        singlecore_baselines=args.singlecore_baselines,
        mode='warm_explicit_charged' if incumbents else 'cold',
        incumbent_ledger_sha256=hashlib.sha256(args.incumbent_ledger.read_bytes()).hexdigest() if args.incumbent_ledger else None,
        incumbents={f'{c}/p{p}_n{n}':dict(path=path, sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest())
                    for (c,p,n),path in incumbents.items()},
        code_commit=subprocess.check_output(['git', '-C', str(R.parent), 'rev-parse', 'HEAD']).decode().strip(),
        sources={str(p.relative_to(R)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(source_files))},
        inputs={c: hashlib.sha256((DATA / (c + '.json')).read_bytes()).hexdigest() for c in cases},
        config=hashlib.sha256((DATA / 'config.txt').read_bytes()).hexdigest())
    upstream_elapsed = 0.
    if args.resume_from:
        previous = args.resume_from.resolve()
        prior = read_json(previous/'manifest.json')
        for key in ('cases','problems','cores','variants','budget','seconds','timeout','seed','stage',
                    'experience_sha256','singlecore_baselines','mode','incumbent_ledger_sha256',
                    'incumbents','inputs','config'):
            assert prior[key] == manifest[key], ('resume changes solver experiment', key)
        # Only orchestration may change; every candidate/search/evaluator source
        # remains byte-identical. The prior source manifest is retained in full.
        orchestration = '直接迭代/run_unified_campaign.py'
        assert {k:v for k,v in prior['sources'].items() if k != orchestration} == {
            k:v for k,v in manifest['sources'].items() if k != orchestration}, 'solver sources changed'
        checkpoint = read_json(previous/'segment_interruption.json')
        assert checkpoint['workers_drained'], 'finish in-flight slots before resuming'
        upstream_elapsed = checkpoint['all_segments_elapsed_seconds']
        reused = {}
        for directory in (previous/'slots').glob('case_*/p*_n*/*'):
            assert (directory/'summary.json').is_file(), ('unfinished slot', str(directory))
            relative = directory.relative_to(previous)
            reused[str(relative)] = hashlib.sha256((directory/'summary.json').read_bytes()).hexdigest()
            target = out/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.symlink_to(directory, target_is_directory=True)
        manifest['resumed_segment'] = dict(path=str(previous), manifest=prior,
            summary_sha256=reused, interruption=checkpoint)
    if (out / 'manifest.json').exists() and read_json(out / 'manifest.json') != manifest:
        raise ValueError('manifest changed; use a new output directory')
    atomic_json(out / 'manifest.json', manifest)
    for path in source_files:
        target = out/'source_snapshot'/path.relative_to(R)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(path, target)
    jobs = [(c, p, n, v, str(out / 'slots' / c / f'p{p}_n{n}' / v), args.budget, args.seconds, args.timeout, args.seed,
             args.stage, str(args.experience.resolve()) if args.experience else None, incumbents.get((c,p,n)))
            for c in cases for n in args.cores for p in args.problems for v in args.variants]
    if args.singlecore_baselines:
        jobs += [(c, 0, 1, 'official_singlecore', str(out/'slots'/c/'p0_n1'/'official_singlecore'),
                  1, args.seconds, args.timeout, args.seed, args.stage, None, None) for c in cases]
    # Size ordering reduces the long-job tail without using scores or graph IDs.
    jobs.sort(key=lambda j: -(DATA / (j[0] + '.json')).stat().st_size)
    rows, errors = [], []
    start = time.monotonic()
    def available_memory():
        try:
            return int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                            if line.startswith('MemAvailable:'))) * 1024
        except (OSError, StopIteration):
            return 2**63
    def heavy(job):
        return (DATA/(job[0]+'.json')).stat().st_size >= 4*1024*1024
    capacity = min(4, args.workers) if args.adaptive_workers else args.workers
    queue = list(jobs)
    concurrency_events = []
    rss_estimate = 512*1024*1024
    rss_per_input_byte = 40.
    reduced_at_size = None
    window_started, window_work, window_done = start, 0., 0
    prior_rate = None
    capacity_frozen = False
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        pending = {}
        while queue or pending:
            while queue and len(pending) < capacity:
                heavy_active = sum(heavy(job) for job in pending.values())
                candidates = [i for i, job in enumerate(queue)
                              if not args.adaptive_workers or not heavy(job) or heavy_active < args.heavy_slots]
                if not candidates:
                    break
                if args.adaptive_workers and pending and available_memory() < 2*rss_estimate:
                    break
                job = queue.pop(candidates[0])
                pending[pool.submit(worker, job)] = job
            completed, _ = wait(pending, timeout=30, return_when=FIRST_COMPLETED)
            if not completed:
                atomic_json(out/'live.json', dict(running=len(pending), queued=len(queue), capacity=capacity,
                    available_memory=available_memory(), elapsed_seconds=time.monotonic()-start))
                continue
            for future in completed:
                job = pending.pop(future)
                try:
                    row = summarize(future.result())
                    rows.append(row)
                    rss_estimate = max(rss_estimate, row['peak_worker_rss'])
                    rss_per_input_byte = max(rss_per_input_byte,
                        max(0, row['peak_worker_rss']-192*1024*1024)/(DATA/(job[0]+'.json')).stat().st_size)
                    window_work += ((DATA/(job[0]+'.json')).stat().st_size/1048576)**1.25 * max(1, row['calls'])
                    print(json.dumps(row, ensure_ascii=False), flush=True)
                except Exception as error:
                    errors.append(dict(job=job[:4], error=repr(error)))
                    print(json.dumps(errors[-1]), flush=True)
                window_done += 1
            if args.adaptive_workers and window_done >= max(4, capacity):
                elapsed = time.monotonic()-window_started
                rate = window_work/max(.01, elapsed)
                # Old peak RSS belongs to a specific graph. Once the remaining
                # pool is smaller, keeping its worst-case estimate permanently
                # can unnecessarily serialize hundreds of small configurations.
                remaining_size = max(((DATA/(j[0]+'.json')).stat().st_size for j in queue+list(pending.values())), default=1)
                current_rss = min(rss_estimate, 192*1024*1024+rss_per_input_byte*remaining_size)
                memory_cap = max(1, int((available_memory()+len(pending)*current_rss)*.65/(1.5*current_rss)))
                if reduced_at_size and remaining_size < reduced_at_size*.5:
                    capacity_frozen, prior_rate, reduced_at_size = False, None, None
                next_capacity = capacity
                if memory_cap < capacity:
                    next_capacity = memory_cap
                elif args.concurrency_policy == 'memory':
                    # Heterogeneous graphs/core counts invalidate a causal
                    # throughput comparison between consecutive windows.
                    next_capacity = min(args.workers, memory_cap, max(4, capacity*2))
                elif not capacity_frozen:
                    if prior_rate is not None and rate < .8*prior_rate:
                        next_capacity = max(2, capacity//2)
                        capacity_frozen = True
                        reduced_at_size = remaining_size
                    else:
                        next_capacity = min(args.workers, memory_cap, capacity*2)
                concurrency_events.append(dict(completed=len(rows), capacity=capacity, next_capacity=next_capacity,
                    normalized_throughput=rate, peak_worker_rss=rss_estimate, memory_cap=memory_cap,
                    remaining_max_input_bytes=remaining_size, predicted_current_worker_rss=current_rss,
                    note='size/call-normalized throughput heuristic; heavy evaluations have a separate slot cap'))
                capacity = next_capacity
                prior_rate = rate
                window_started, window_work, window_done = time.monotonic(), 0., 0
                atomic_json(out/'concurrency.json', concurrency_events)
            write_csv(out / 'results.csv', sorted(rows, key=lambda r: (r['case'], r['problem'], r['cores'], r['variant'])))
            atomic_json(out / 'progress.json', dict(completed=len(rows), total=len(jobs), errors=errors,
                elapsed_seconds=time.monotonic() - start))
    result = dict(completed=len(rows), total=len(jobs), errors=errors, elapsed_seconds=time.monotonic() - start,
        all_segments_elapsed_seconds=upstream_elapsed+time.monotonic()-start,
        official_calls=sum(r['new_calls'] for r in rows), valid_slots=sum(r['valid'] for r in rows),
        interpretation='Improvement vs evaluated seeds is mechanism evidence, not equal-budget baseline dominance.')
    atomic_json(out / 'completion.json', result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    ints = lambda s: [int(x) for x in s.split(',')]
    p.add_argument('--cases', type=ints, default=list(range(1, 101)))
    p.add_argument('--problems', type=ints, default=[1, 2, 3])
    p.add_argument('--cores', type=ints, default=[5])
    p.add_argument('--variants', type=lambda s: s.split(','), default=['v2'])
    p.add_argument('--budget', type=int, default=8)
    p.add_argument('--seconds', type=float, default=180)
    p.add_argument('--timeout', type=float, default=60)
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--adaptive-workers', action='store_true')
    p.add_argument('--concurrency-policy', choices=('memory','throughput'), default='memory')
    p.add_argument('--resume-from', type=Path, help='drained segment; identical solver, inputs and budgets required')
    p.add_argument('--heavy-slots', type=int, default=2)
    p.add_argument('--singlecore-baselines', action='store_true')
    p.add_argument('--incumbent-ledger', type=Path, help='explicit warm input index; first evaluation is charged')
    p.add_argument('--incumbent-root', type=Path, help='root for relative plan paths in explicit ledger')
    p.add_argument('--seed', type=int, default=17)
    p.add_argument('--stage', choices=('A', 'B'), default='B')
    p.add_argument('--experience', type=Path)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    if any(c not in range(1, 101) for c in a.cases) or any(p not in (1, 2, 3) for p in a.problems) or any(n not in range(1, 6) for n in a.cores):
        p.error('invalid graph/scene/core range')
    if min(a.budget, a.seconds, a.timeout, a.workers) <= 0:
        p.error('positive budget/time/workers required')
    print(json.dumps(run(a), ensure_ascii=False))


if __name__ == '__main__':
    main()
