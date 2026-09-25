"""Frozen two-round attack panel; historical costs stay separate and explicit."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import time

from common_run import DATA, GraphIR, atomic_json, evaluate, read_json, score, validate_plan, write_csv
from unified_structure import Structure
from unified_solver import signature


def run_slot(item, out, round_index, seconds, timeout):
    started = time.monotonic()
    dest = Path(out)/'slots'/item['case']/f"p{item['problem']}_n{item['cores']}"/'attack'
    dest.mkdir(parents=True, exist_ok=False)
    graph = DATA/(item['case']+'.json')
    ir, original = GraphIR.from_path(graph), read_json(item['plan'])
    validate_plan(ir, original)
    assert hashlib.sha256(Path(item['plan']).read_bytes()).hexdigest() == item['plan_sha256']
    s = Structure(ir)
    calls, rejected, seen = [], [], {signature(original)}
    rec = evaluate(graph, original, item['problem'], dest/'evaluations', timeout=90, config_path=DATA/'config.txt')
    calls.append(dict(name='charged_incumbent_replay', record=rec, accepted=True))
    assert rec['status']=='success' and rec['metrics']['makespan']==item['makespan'], (item['case'],item['problem'],rec['status'],rec.get('error'))
    best, name = rec, 'incumbent'
    from aggressive_search import round_one
    if round_index == 1:
        generator = round_one(s, item['problem'], item['cores'], original, started+seconds)
    else:
        from aggressive_search import round_two
        generator = round_two(s, item['problem'], item['cores'], original, rec, started+seconds,item.get('donors',()))
    while True:
        if time.monotonic() >= started+seconds or len(calls) >= 9:
            break
        try:
            c=next(generator)
        except StopIteration:
            break
        except (TimeoutError,ValueError) as error:
            rejected.append(dict(reason='generation_stopped',error=str(error))); break
        identity = signature(c['plan'])
        if identity in seen:
            rejected.append(dict(name=c['name'],reason='duplicate')); continue
        seen.add(identity)
        try:
            validate_plan(ir,c['plan'])
        except ValueError as error:
            rejected.append(dict(name=c['name'],reason='invalid_structure',error=str(error))); continue
        r = evaluate(graph,c['plan'],item['problem'],dest/'evaluations',
                     timeout=min(timeout,max(.1,started+seconds-time.monotonic())),config_path=DATA/'config.txt')
        accepted = r['status']=='success' and score(r)<score(best)
        calls.append(dict(name=c['name'],metadata=c['metadata'],record=r,accepted=accepted))
        if accepted:
            best,name=r,c['name']
        atomic_json(dest/'progress.json',dict(calls=len(calls),best=best,best_name=name))
    result=dict(case=item['case'],problem=item['problem'],num_cores=item['cores'],round=round_index,
        mode='explicit_warm_charged',upstream=item,before=item['makespan'],after=best['metrics']['makespan'],
        reduction=1-best['metrics']['makespan']/item['makespan'],best_record=best,best_name=name,
        evaluations=calls,new_calls=sum(not x['record']['cache_hit'] for x in calls),
        failures=sum(x['record']['status']!='success' for x in calls),returned_valid=True,
        rejected=rejected,elapsed_seconds=time.monotonic()-started)
    atomic_json(dest/'summary.json',result)
    return {k:result[k] for k in ('case','problem','num_cores','before','after','reduction','best_name','new_calls','failures','elapsed_seconds')}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--panel',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--round',type=int,choices=(1,2),required=True)
    p.add_argument('--workers',type=int,default=6)
    p.add_argument('--seconds',type=float,default=150)
    p.add_argument('--timeout',type=float,default=35)
    a=p.parse_args(); a.out.mkdir(parents=True,exist_ok=False)
    items=read_json(a.panel)['items']; started=time.monotonic()
    manifest=dict(panel=str(a.panel.resolve()),panel_sha256=hashlib.sha256(a.panel.read_bytes()).hexdigest(),
        code_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        round=a.round,seconds=a.seconds,timeout=a.timeout,calls_per_slot=9,workers=a.workers,
        criterion='Retained mean time reduction >= 3% across all 30 cells and >= 3 distinct graphs with >= 5% reduction. Development evidence only.')
    atomic_json(a.out/'manifest.json',manifest)
    rows,errors=[],[]
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        jobs={pool.submit(run_slot,x,str(a.out.resolve()),a.round,a.seconds,a.timeout):(x['case'],x['problem']) for x in items}
        for f in as_completed(jobs):
            try:
                row=f.result(); rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
            except Exception as e:
                errors.append(dict(key=jobs[f],error=repr(e)));print(json.dumps(errors[-1]),flush=True)
            atomic_json(a.out/'progress.json',dict(completed=len(rows),errors=errors,total=len(items)))
    write_csv(a.out/'逐配置.csv',sorted(rows,key=lambda r:(r['case'],r['problem'])))
    groups=[dict(problem=p,cells=sum(r['problem']==p for r in rows),
        mean_reduction=statistics.mean(r['reduction'] for r in rows if r['problem']==p),
        wins=sum(r['problem']==p and r['reduction']>0 for r in rows)) for p in (1,2,3)]
    mean=statistics.mean(r['reduction'] for r in rows) if rows else 0
    substantial=sorted({r['case'] for r in rows if r['reduction']>=.05})
    result=dict(completed=len(rows),total=len(items),errors=errors,groups=groups,
        mean_reduction=mean,graphs_over_five_percent=substantial,
        obvious_improvement=len(rows)==len(items) and not errors and mean>=.03 and len(substantial)>=3,
        new_calls=sum(r['new_calls'] for r in rows),failures=sum(r['failures'] for r in rows),
        elapsed_seconds=time.monotonic()-started)
    atomic_json(a.out/'completion.json',result);print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
