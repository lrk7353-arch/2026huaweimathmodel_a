"""Accept new panel winners only after independent official replay."""
import csv,gzip,hashlib,json,shutil,sys,time
from concurrent.futures import ProcessPoolExecutor,as_completed
from pathlib import Path
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parent))
from common_run import DATA,run_candidate,read_json,atomic_json,write_csv,score

def replay(item):
    key=f'{item["case"]}_p{item["problem"]}_n{item["cores"]}'
    plan=read_json(item["record"]["plan_path"])
    r=run_candidate(item["case"],item["problem"],item["cores"],plan,HERE/"新增精选复评"/"复评运行"/key,timeout=300)
    ok=r["status"]=="success" and score(r)==(item["after"],item["after_copy"])
    return dict(**item,replay_record=r,replay_matches=ok)

def main():
    root=HERE/"同预算对照";winners=read_json(root/"待复评新增精选.json")["winners"]
    out=HERE/"最终累计";out.mkdir(exist_ok=False)
    with (HERE/"累计并集/累计1500配置成绩.csv").open(encoding="utf-8-sig") as f:rows=list(csv.DictReader(f))
    results=[]
    with ProcessPoolExecutor(max_workers=6) as pool:
        for r in pool.map(replay,winners):
            results.append(r)
            print(json.dumps({k:r[k] for k in ("case","problem","cores","before","after","variant","replay_matches")}),flush=True)
    indexed={(r["case"],r["problem"],r["cores"]):r for r in results if r["replay_matches"]}
    for row in rows:
        key=row["case"],int(row["problem"]),int(row["cores"])
        if key in indexed:
            r=indexed[key];plan=read_json(r["replay_record"]["plan_path"])
            row.update(makespan=r["after"],added_copy=r["after_copy"],speedup=int(row["original_singlecore"])/r["after"],
                source="joint_panel_"+r["variant"],source_commit=read_json(root/"manifest.json")["git_commit"],
                source_plan=r["summary"],verification="fresh_panel_winner_replay")
        else:plan=read_json(HERE/"累计并集"/row["plan"])
        raw=json.dumps(plan,ensure_ascii=False,separators=(",",":")).encode()
        path=out/row["plan"];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        row["plan_sha256"]=hashlib.sha256(raw).hexdigest()
    write_csv(out/"累计1500配置成绩.csv",rows)
    groups=[]
    for p in (1,2,3):
        for n in range(1,6):
            rs=[r for r in rows if (int(r["problem"]),int(r["cores"]))==(p,n)]
            groups.append(dict(problem=p,cores=n,cases=len(rs),mean_speedup=sum(float(r["speedup"]) for r in rs)/len(rs)))
    write_csv(out/"各核数汇总.csv",groups)
    # A small portable archive accompanies the exact JSON files.
    for p in (1,2,3):
        for n in range(1,6):
            bundle={r["plan"]:read_json(out/r["plan"]) for r in rows if (int(r["problem"]),int(r["cores"]))==(p,n)}
            folder=out/"方案分包";folder.mkdir(exist_ok=True)
            with gzip.open(folder/f"p{p}_n{n}.json.gz","wt",encoding="utf-8") as f:json.dump(bundle,f,ensure_ascii=False,separators=(",",":"))
    atomic_json(HERE/"新增精选复评/结果.json",dict(attempted=len(results),passed=sum(r["replay_matches"] for r in results),records=results))
    summary=dict(coverage=len(rows),fresh_additions=len(indexed),attempted=len(results),
        strict_time_additions=sum(r["after"]<r["before"] for r in indexed.values()),
        same_time_copy_additions=sum(r["after"]==r["before"] for r in indexed.values()),
        scope="cumulative historical union plus independently replayed panel winners; not cold1500")
    atomic_json(out/"summary.json",summary)
    print(json.dumps(dict(summary=summary,groups=groups),ensure_ascii=False,indent=2))
if __name__=="__main__":main()
