"""Run the registered joint/strong cold comparison, preserving every paid call."""
import argparse, csv, hashlib, json, random, subprocess, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from common_run import DATA,R,read_json,atomic_json,write_csv,score
from joint_solver import run

def worker(job):
    case,p,n,variant,group,out,cfg=job
    slot=Path(out)/"slots"/group/case/f"p{p}_n{n}"/variant
    path=slot/"summary.json"
    if path.exists():s=read_json(path)
    else:s=run(case,p,n,slot,cfg["budget"],cfg["seconds"],cfg["evaluation_timeout"],variant)
    calls=s["calls"];rec=s["best_record"];prefixes={}
    for cap in cfg["prefixes"]:
        good=[x["record"] for x in calls[:cap] if x["record"]["status"]=="success"]
        prefixes[f"time_b{cap}"]=min(map(score,good))[0] if good else None
    ps=read_json(s["prefix_summary"])
    gen=sum(x.get("generation_seconds",x.get("seconds",0)) for x in ps.get("stages",[]) if isinstance(x,dict))
    return dict(case=case,problem=p,cores=n,variant=variant,group=group,
        status=s["status"],makespan=score(rec)[0] if rec else None,added_copy=score(rec)[1] if rec else None,
        logical_calls=len(calls),new_calls=s["new_calls"],failures=sum(x["record"]["status"]!="success" for x in calls),
        timeouts=sum(x["record"]["status"]=="timeout" for x in calls),elapsed_seconds=s["elapsed_seconds"],
        tail_generation_seconds=s["tail_generation_seconds"],prefix_reported_generation_seconds=gen,
        prefix_elapsed_seconds=s["prefix_elapsed_seconds"],stop_reason=s["stop_reason"],
        summary=str(path),**prefixes)

def main():
    p=argparse.ArgumentParser();p.add_argument("--protocol",type=Path,required=True);p.add_argument("--out",type=Path,required=True)
    a=p.parse_args();cfg=read_json(a.protocol);out=a.out.resolve();out.mkdir(parents=True,exist_ok=True)
    hashes={}
    for folder in (R/"solver",R/"advanced_solver",R/"精修求解器",R/"探索",Path(__file__).parent):
        for f in folder.glob("*.py"):hashes[str(f.relative_to(R.parent))]=hashlib.sha256(f.read_bytes()).hexdigest()
    manifest=dict(protocol=cfg,source_hashes=hashes,git_commit=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip())
    if (out/"manifest.json").exists():
        old=read_json(out/"manifest.json")
        assert old["protocol"]==cfg and old["source_hashes"]==hashes,"Do not mix source versions"
    else:atomic_json(out/"manifest.json",manifest)
    jobs=[]
    for group,ids,cores in (("development",cfg["development_cases"],[5]),("validation",cfg["validation_cases"],[5]),("lowcore",cfg["low_core_cases"],cfg["low_cores"])):
        for i in ids:
            for problem in cfg["problems"]:
                for n in cores:
                    for variant in cfg["variants"]:jobs.append((f"case_{i:03d}",problem,n,variant,group,str(out),cfg))
    random.Random(17).shuffle(jobs)
    started=time.monotonic();rows=[];errors=[]
    with ProcessPoolExecutor(max_workers=cfg["workers"]) as pool:
        futures={pool.submit(worker,j):j for j in jobs}
        for future in as_completed(futures):
            job=futures[future]
            try:
                row=future.result();rows.append(row)
                print(json.dumps({k:row[k] for k in ("case","problem","cores","variant","group","makespan","logical_calls","elapsed_seconds")}),flush=True)
            except Exception as exc:
                errors.append(dict(job=list(job[:5]),error=repr(exc)));print("ERROR "+repr(exc),flush=True)
            write_csv(out/"逐臂成绩.csv",rows)
            atomic_json(out/"progress.json",dict(done=len(rows),expected=len(jobs),errors=errors,elapsed_seconds=time.monotonic()-started))
    atomic_json(out/"execution.json",dict(complete=len(rows)==len(jobs),expected=len(jobs),completed=len(rows),errors=errors,
        elapsed_seconds=time.monotonic()-started,new_calls=sum(r["new_calls"] for r in rows),
        failures=sum(r["failures"] for r in rows),workers=cfg["workers"]))
    assert len(rows)==len(jobs),errors
if __name__=="__main__":main()
