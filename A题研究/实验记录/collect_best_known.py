"""Collect saved, officially evaluated plans across explicitly named experiments.

This is an auditable best-known portfolio, NOT a new independently benchmarked
algorithm or a claim of global optimality. Retains provenance for each selection.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import gzip
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(RESEARCH / "solver"))
from common import DATA, OFFICIAL, atomic_json, digest, object_digest, read_json


def collect(benchmark_runs, exploratory_runs, output):
    output=Path(output).resolve()
    source_hashes={p.name:digest(p) for p in sorted(OFFICIAL.glob("*.py"))}
    config_hash=digest(DATA/"config.txt")
    graph_hashes={p.stem:digest(p) for p in sorted(DATA.glob("case_*.json"))}
    checked_results={}
    selected={}
    rejected=[]
    out_of_scope=[]
    pool=[]

    def add(case,problem,cores,plan,record,origin):
        if record.get("status") != "success":return
        try:
            hashes=record["hashes"]
            if hashes["graph_sha256"] != graph_hashes[case]:
                raise ValueError("graph hash mismatch")
            if hashes["config_sha256"] != config_hash or hashes["official_py_sha256"] != source_hashes:
                raise ValueError("official source/config mismatch")
            if len(plan["core_schedules"]) != cores or record["problem"] != problem:
                raise ValueError("scenario/core mismatch")
            serialized=json.dumps(plan,ensure_ascii=False,allow_nan=False,separators=(",",":")).encode()
            if hashlib.sha256(serialized).hexdigest()!=hashes["plan_sha256"]:
                raise ValueError("plan does not match evaluated bytes")
            if digest(record["result_path"]) != record["result_sha256"]:
                raise ValueError("official result file missing/changed")
            metrics=record["metrics"]
            signature=(record["result_path"],record["result_sha256"])
            if signature not in checked_results:
                with gzip.open(record["result_path"],"rt",encoding="utf-8") as stream:
                    raw=json.load(stream)
                checked_results[signature]={k:raw.get(k) for k in ("makespan","num_cores","data_movement_bytes","cache_stats")}
            actual=checked_results[signature]
            if actual["makespan"]!=metrics["makespan"] or actual["num_cores"]!=cores:
                raise ValueError("metadata score/core differs from raw official output")
            for field in ("data_movement_bytes","cache_stats"):
                if actual[field] is not None and actual[field]!=metrics.get(field):
                    raise ValueError("metadata differs from raw official "+field)
            item={"case":case,"problem":problem,"num_cores":cores,"plan":plan,
                  "makespan":metrics["makespan"],
                  "added_copy_bytes":metrics.get("data_movement_bytes",{}).get("added_copy_bytes",0),
                  "active_cores":sum(bool(c) for c in plan["core_schedules"]),
                  "origin":str(origin),"evaluation_record":record}
            pool.append({k:v for k,v in item.items() if k not in ("plan","evaluation_record")})
            key=(case,problem,cores)
            if key not in selected or (item["makespan"],item["added_copy_bytes"]) < (selected[key]["makespan"],selected[key]["added_copy_bytes"]):
                selected[key]=item
        except (KeyError,OSError,ValueError) as error:
            rejected.append({"case":case,"origin":str(origin),"error":str(error)})

    for run in benchmark_runs:
        for path in sorted((Path(run)/"results").glob("case_*/*_p*_n*_seed*.json")):
            if path.name.endswith(".plan.json"):continue
            result=read_json(path)
            if result.get("best"):
                add(result["case"],result["problem"],result["num_cores"],result["best"]["plan"],result["best"]["record"],path)
    for run in exploratory_runs:
        # Use the stable evaluator ledger schema, independent of experiment-specific
        # summaries. The complete plan and official-result hashes are checked above.
        for path in sorted(Path(run).rglob("record.json")):
            record=read_json(path)
            if record.get("status")!="success" or record.get("problem") not in (1,2,3):continue
            plan_path=record.get("plan_path")
            if plan_path and Path(plan_path).exists():
                plan=read_json(plan_path)
                case=Path(record["graph_path"]).stem
                if case not in graph_hashes:
                    out_of_scope.append({"case":case,"origin":str(path),"reason":"synthetic/non-official graph outside the 100-case portfolio"})
                    continue
                add(case,record["problem"],len(plan["core_schedules"]),plan,record,path)
    output.mkdir(parents=True,exist_ok=True)
    rows=[]
    from graph_ir import GraphIR
    from plan import validate_plan
    current_case,current_ir=None,None
    for (case,problem,cores),item in sorted(selected.items()):
        if case != current_case:
            current_case,current_ir=case,GraphIR.from_path(DATA/(case+".json"))
        validate_plan(current_ir,item["plan"])
        target=output/f"p{problem}"/f"n{cores}"/(case+"_multicore_res.json")
        atomic_json(target,item["plan"])
        row={k:v for k,v in item.items() if k not in ("plan","evaluation_record")}
        row["plan_path"]=str(target)
        row["plan_sha256"]=digest(target)
        row["official_result_path"]=item["evaluation_record"]["result_path"]
        rows.append(row)
        atomic_json(target.with_suffix(".provenance.json"),{**row,"evaluation_record":item["evaluation_record"]})
    fields=["case","problem","num_cores","makespan","added_copy_bytes","active_cores","origin","plan_path","plan_sha256","official_result_path"]
    with (output/"catalog.csv").open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(rows)
    manifest={"scope":"best known across finite saved portfolios; not an independently timed solver comparison",
              "benchmark_runs":[str(Path(p).resolve()) for p in benchmark_runs],
              "exploratory_runs":[str(Path(p).resolve()) for p in exploratory_runs],
              "selected_count":len(rows),"coverage":dict(Counter(f"p{r['problem']}_n{r['num_cores']}" for r in rows)),
              "source_hashes":source_hashes,"config_sha256":config_hash,"rejected":rejected,"pool":pool}
    manifest["collection_source_sha256"]=digest(__file__)
    manifest["verification"]="All candidate official gzip hashes and raw metrics verified; all selected plans structurally validated"
    manifest["out_of_scope"]=out_of_scope
    manifest["expected_full_coverage"]=1500
    manifest["full_coverage"]=len(rows)==1500
    atomic_json(output/"manifest.json",manifest)
    (output/"README.md").write_text("# 当前已验证最好方案\n\n"
        "此目录汇总已保存实验中、经同一原版官方配置评估的最好方案，不代表全局最优或新的同预算算法成绩。"
        "先比较makespan，完全相同再比较新增搬运量。每份计划旁有来源与官方结果记录；原实验不覆盖。\n\n"
        "覆盖范围见manifest.json，逐图指标见catalog.csv。缺少的核数/场景未自动补成绩。\n",encoding="utf-8")
    if rejected:raise RuntimeError(f"{len(rejected)} candidate records rejected; inspect manifest.json")
    return manifest


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--benchmark",type=Path,action="append",default=[])
    p.add_argument("--exploratory",type=Path,action="append",default=[])
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    result=collect(args.benchmark,args.exploratory,args.output)
    print(json.dumps({"selected":result["selected_count"],"coverage":result["coverage"]},ensure_ascii=False))
