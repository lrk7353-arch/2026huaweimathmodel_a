"""Build a provenance-preserving union; replay every newly imported objective."""
import argparse, csv, gzip, hashlib, json, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from common_run import DATA, R, GraphIR, validate_plan, evaluate, score, read_json, atomic_json, write_csv

DIRECT=Path(__file__).resolve().parent
AUDIT=DIRECT/"队友成果对比_20260925"
DELIVERY=DIRECT/"全场景架构研究_20260925/批次C_累计精选交付"
TEAM=DIRECT/"第十三轮成果"
OUR_COMMIT="238aafecf447488e1397467ddebe4d994477c899"
TEAM_COMMIT="8f3837f18546a015cb75ac992e870e51f48659c1"

@lru_cache(maxsize=2)
def graph(case):
    return GraphIR.from_path(DATA/(case+".json"))

def check_case(job):
    case,entries,out=job
    ir=graph(case)
    for row in entries:
        plan=read_json(Path(out)/row["plan"])
        validate_plan(ir,plan)
        assert len(plan["core_schedules"])==row["cores"]
    return len(entries)

def replay(job):
    row,out=job
    path=Path(out)/row["plan"]
    record=evaluate(DATA/(row["case"]+".json"),read_json(path),row["problem"],
                    Path(out)/"复评运行"/f'{row["case"]}_p{row["problem"]}_n{row["cores"]}',
                    timeout=300,config_path=DATA/"config.txt")
    expected=(row["makespan"],row["added_copy"])
    matched=record["status"]=="success" and score(record)==expected
    return dict(case=row["case"],problem=row["problem"],cores=row["cores"],
                matched=matched,status=record["status"],cache_hit=record["cache_hit"],
                expected=list(expected),actual=list(score(record)) if record["status"]=="success" else None,
                elapsed_seconds=record["elapsed_seconds"],record=record)

def build(out,workers):
    started=time.monotonic();out=Path(out).resolve()
    out.mkdir(parents=True,exist_ok=False)
    with (AUDIT/"逐配置对账.csv").open(encoding="utf-8-sig") as f: pairs=list(csv.DictReader(f))
    bundles={}
    for path in sorted((DELIVERY/"方案分包").glob("*.json.gz")):
        with gzip.open(path,"rt") as f:bundles.update(json.load(f))
    rows=[]
    for pair in pairs:
        case=pair["case"];p=int(pair["problem"]);n=int(pair["cores"]);choice=pair["chosen"]
        if choice=="ours":
            source=pair["ours_plan"]
            if pair["ours_plan_base"]=="delivery":
                plan=bundles[source]
            else:plan=read_json(R.parent/source)
        else:
            source="A题研究/直接迭代/第十三轮成果/"+pair["team13_plan"]
            plan=read_json(R.parent/source)
        raw=json.dumps(plan,ensure_ascii=False,allow_nan=False,separators=(",",":")).encode()
        relative=f"方案/p{p}/n{n}/{case}_multicore_res.json"
        target=out/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
        rows.append(dict(case=case,problem=p,cores=n,makespan=int(pair["union_time"]),
            added_copy=int(pair["union_copy"]),original_singlecore=int(pair["original_singlecore"]),
            speedup=float(pair["union_speedup"]),source=choice,source_commit=OUR_COMMIT if choice=="ours" else TEAM_COMMIT,
            source_plan=source,plan=relative,plan_sha256=hashlib.sha256(raw).hexdigest(),
            verification="pending_import_replay" if choice=="ours" else "inherited_team13_success"))
    assert len(rows)==1500 and sum(r["source"]=="ours" for r in rows)==111
    write_csv(out/"累计1500配置成绩.csv",rows)
    grouped=[(f"case_{i:03d}",[r for r in rows if r["case"]==f"case_{i:03d}"],str(out)) for i in range(1,101)]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        assert sum(pool.map(check_case,grouped))==1500
    atomic_json(out/"结构检查.json",dict(plans=1500,valid=1500,original_ops_coverage=True))
    jobs=[(r,str(out)) for r in rows if r["source"]=="ours"]
    results=[]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures=[pool.submit(replay,j) for j in jobs]
        for future in as_completed(futures):
            item=future.result();results.append(item)
            atomic_json(out/"复评记录.json",dict(records=results))
            atomic_json(out/"progress.json",dict(done=len(results),expected=len(jobs),
                matched=sum(r["matched"] for r in results),elapsed_seconds=time.monotonic()-started))
            print(json.dumps({k:v for k,v in item.items() if k!="record"},ensure_ascii=False),flush=True)
    indexed={(r["case"],r["problem"],r["cores"]):r for r in results}
    for row in rows:
        k=row["case"],row["problem"],row["cores"]
        if k in indexed:
            row["verification"]="fresh_import_replay" if indexed[k]["matched"] else "FAILED_REPLAY"
    write_csv(out/"累计1500配置成绩.csv",rows)
    groups=[]
    for p in (1,2,3):
        for n in range(1,6):
            rs=[r for r in rows if (r["problem"],r["cores"])==(p,n)]
            groups.append(dict(problem=p,cores=n,cases=len(rs),mean_speedup=sum(r["speedup"] for r in rs)/len(rs)))
    write_csv(out/"各核数汇总.csv",groups)
    result=dict(complete=all(r["matched"] for r in results),coverage=len(rows),imported=111,
        fresh_calls=sum(not r["cache_hit"] for r in results),matched=sum(r["matched"] for r in results),
        failures=sum(not r["matched"] for r in results),workers=workers,elapsed_seconds=time.monotonic()-started,
        scope="union of historical evaluated plans; 111 imported objectives replayed; remaining 1389 inherited")
    atomic_json(out/"summary.json",result);print(json.dumps(result),flush=True)
    assert result["complete"],"Do not publish unverified union as complete"

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--out",type=Path,required=True);p.add_argument("--workers",type=int,default=6)
    a=p.parse_args();build(a.out,a.workers)
