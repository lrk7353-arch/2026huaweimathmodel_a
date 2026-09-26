"""Development comparison against the frozen latest library; NOT cold scores."""
import argparse
import concurrent.futures
import csv
import hashlib
import json
from pathlib import Path
import time

from common_run import DATA, GraphIR, atomic_json, evaluate, read_json, score
from event_frontier import candidates

HERE = Path(__file__).resolve().parent
LEDGER = HERE/'三指标联合推进_20260926/累计1500配置成绩.csv'


def run_one(task):
    row, out, budget, seconds, timeout, generator, ledger = task
    out = Path(out); out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic(); deadline = started + seconds
    case, problem, cores = row['case'], int(row['problem']), int(row['cores'])
    ir = GraphIR.from_path(DATA/(case+'.json'))
    parent = read_json(Path(ledger).parent/row['plan'])
    calls, errors, seen = [], [], set()
    best = None; generation_seconds = 0.
    def checkpoint(complete=False):
        result = dict(case=case,problem=problem,cores=cores,scope='warm development against latest cumulative library',
            budget=budget,seconds=seconds,ledger_baseline=int(row['makespan']),calls=calls,best_record=best,
            generation_seconds=generation_seconds,generation_errors=errors,complete=complete,
            elapsed_seconds=time.monotonic()-started)
        atomic_json(out/('summary.json' if complete else 'progress.json'),result)
        return result
    def apply(candidate):
        nonlocal best
        signature=json.dumps(candidate['plan'],ensure_ascii=False,separators=(',',':'))
        if signature in seen:return
        seen.add(signature)
        record=evaluate(DATA/(case+'.json'),candidate['plan'],problem,out/'evaluations',
                        timeout=min(timeout,max(.001,deadline-time.monotonic())),config_path=DATA/'config.txt')
        accepted=record['status']=='success' and (best is None or score(record)<score(best))
        calls.append(dict(name=candidate['name'],metadata=candidate.get('metadata',{}),record=record,accepted=accepted))
        if accepted:
            best=record;atomic_json(out/'best.plan.json',candidate['plan'])
        checkpoint()
    apply(dict(name='charged_library_start',plan=parent))
    if best is not None and score(best)[0]!=int(row['makespan']):
        raise ValueError(f'Frozen library baseline mismatch: {case}/P{problem}')
    if generator=='backward':
        from backward_frontier import candidates as build
    elif generator=='barrier':
        from barrier_bands import candidates as build
    elif generator=='residency':
        from residency_frontier import candidates as build
    else:build=candidates
    if generator in ('contract','cache_gap'):
        import gzip
        if generator=='contract':
            from critical_contract import candidates as build
        else:
            from cache_gap_link import candidates as build
        with gzip.open(best['result_path'],'rt') as f:raw=json.load(f)
        stream=build(ir,problem,cores,parent,raw,deadline)
    else:stream=build(ir,problem,cores,parent,deadline)
    while len(calls)<budget and time.monotonic()<deadline:
        t=time.monotonic()
        try:
            proposal=next(stream)
        except StopIteration:
            generation_seconds+=time.monotonic()-t;break
        except Exception as exc:
            generation_seconds+=time.monotonic()-t;errors.append(repr(exc));break
        generation_seconds+=time.monotonic()-t
        if time.monotonic()<deadline:apply(proposal)
    return checkpoint(True)


def main():
    p=argparse.ArgumentParser();p.add_argument('--cases',required=True)
    p.add_argument('--problems',default='1');p.add_argument('--cores',type=int,default=5)
    p.add_argument('--budget',type=int,default=29);p.add_argument('--seconds',type=float,default=240)
    p.add_argument('--timeout',type=float,default=45);p.add_argument('--workers',type=int,default=4)
    p.add_argument('--generator',choices=('forward','backward','barrier','residency','contract','cache_gap'),default='forward')
    p.add_argument('--ledger',type=Path,default=LEDGER)
    p.add_argument('--out',type=Path,required=True);args=p.parse_args()
    cases={f'case_{int(c):03d}' for c in args.cases.split(',')}
    problems={int(v) for v in args.problems.split(',')}
    rows=[r for r in csv.DictReader(args.ledger.open(encoding='utf-8-sig'))
          if r['case'] in cases and int(r['problem']) in problems and int(r['cores'])==args.cores]
    args.out.mkdir(parents=True,exist_ok=False)
    atomic_json(args.out/'manifest.json',dict(scope='development; historical library is a charged warm start, not a free cold seed',
        cases=sorted(cases),problems=sorted(problems),cores=args.cores,budget=args.budget,seconds=args.seconds,
        workers=args.workers,timeout=args.timeout,generator=args.generator,ledger_sha256=hashlib.sha256(args.ledger.read_bytes()).hexdigest(),
        source_sha256={name:hashlib.sha256((HERE/name).read_bytes()).hexdigest()
                       for name in ['event_frontier.py','run_frontier_probe.py']+({'backward':['backward_frontier.py'],
                           'barrier':['barrier_bands.py'],'contract':['critical_contract.py','barrier_bands.py'],
                           'residency':['residency_frontier.py','p23_data_refine.py'],
                           'cache_gap':['cache_gap_link.py','p3_joint_reads.py']}.get(args.generator,[]))}))
    tasks=[(r,str(args.out/r['case']/('p'+r['problem'])),args.budget,args.seconds,args.timeout,args.generator,str(args.ledger)) for r in rows]
    results=[]
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(run_one,tasks):
            best=result['best_record']
            compact=dict(case=result['case'],problem=result['problem'],baseline=result['ledger_baseline'],
                best=score(best)[0] if best else None,calls=len(result['calls']),generation_seconds=result['generation_seconds'],
                elapsed_seconds=result['elapsed_seconds'],errors=result['generation_errors'])
            results.append(compact);atomic_json(args.out/'results.json',results)
            print(json.dumps(compact),flush=True)


if __name__=='__main__':main()
