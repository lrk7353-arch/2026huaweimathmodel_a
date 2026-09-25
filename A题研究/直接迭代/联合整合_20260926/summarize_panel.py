"""Summarize the frozen comparison and preserve portable per-call evidence."""
import csv,gzip,hashlib,json,statistics,sys
from pathlib import Path
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parent))
from common_run import read_json,atomic_json,write_csv,score
def main():
    root=HERE/"同预算对照";cfg=read_json(HERE/"执行协议.json")
    with (root/"逐臂成绩.csv").open(encoding="utf-8-sig") as f:rows=list(csv.DictReader(f))
    execution=read_json(root/"execution.json");assert execution["complete"] and len(rows)==216
    bykey={(r["case"],int(r["problem"]),int(r["cores"]),r["group"],r["variant"]):r for r in rows}
    pairs=[]
    for key,a in bykey.items():
        case,p,n,group,variant=key
        if variant!="strong":continue
        b=bykey[case,p,n,group,"joint"]
        valid=bool(a["makespan"] and b["makespan"])
        x,y=(int(a["makespan"]),int(b["makespan"])) if valid else (None,None)
        pairs.append(dict(case=case,problem=p,cores=n,group=group,strong_time=x,joint_time=y,
            winner="invalid" if not valid else "joint" if y<x else "strong" if x<y else "tie",
            reduction_pct=100*(1-y/x) if valid else None,
            strong_calls=int(a["logical_calls"]),joint_calls=int(b["logical_calls"]),
            strong_seconds=float(a["elapsed_seconds"]),joint_seconds=float(b["elapsed_seconds"]),
            strong_failures=int(a["failures"]),joint_failures=int(b["failures"])))
    write_csv(root/"逐配置比较.csv",pairs)
    groups=[]
    for group in ("development","validation","lowcore"):
        for p in (1,2,3):
            rs=[r for r in pairs if r["group"]==group and r["problem"]==p]
            good=[r for r in rs if r["winner"]!="invalid"]
            groups.append(dict(group=group,problem=p,configs=len(rs),valid_pairs=len(good),
                joint_wins=sum(r["winner"]=="joint" for r in rs),ties=sum(r["winner"]=="tie" for r in rs),
                strong_wins=sum(r["winner"]=="strong" for r in rs),
                mean_paired_reduction_pct=statistics.mean(r["reduction_pct"] for r in good) if good else None,
                worst_regression_pct=max([0]+[-r["reduction_pct"] for r in good]),
                mean_joint_seconds=statistics.mean(r["joint_seconds"] for r in rs),
                mean_strong_seconds=statistics.mean(r["strong_seconds"] for r in rs),
                joint_calls=sum(r["joint_calls"] for r in rs),strong_calls=sum(r["strong_calls"] for r in rs),
                joint_failures=sum(r["joint_failures"] for r in rs),strong_failures=sum(r["strong_failures"] for r in rs)))
    write_csv(root/"分组汇总.csv",groups)
    gates=[];rule=cfg["gate"]
    for p in (1,2,3):
        v=next(g for g in groups if (g["group"],g["problem"])==("validation",p))
        low=next(g for g in groups if (g["group"],g["problem"])==("lowcore",p))
        invalid_new=any(r["problem"]==str(p) and r["variant"]=="joint" and not r["makespan"] for r in rows)
        checks=dict(validation_mean=v["mean_paired_reduction_pct"] is not None and v["mean_paired_reduction_pct"]>=rule["per_problem_validation_mean_time_reduction_pct_at_least"],
            validation_wins=v["joint_wins"]>=rule["per_problem_validation_strict_wins_at_least"],
            validation_worst=v["worst_regression_pct"]<=rule["per_problem_validation_worst_regression_pct_at_most"],
            lowcore_mean=low["mean_paired_reduction_pct"] is not None and low["mean_paired_reduction_pct"]>=0,
            valid=not invalid_new and v["valid_pairs"]==v["configs"] and low["valid_pairs"]==low["configs"])
        gates.append(dict(problem=p,passed=all(checks.values()),checks=checks))
    decision=dict(scenes=gates,run_final1500=all(g["passed"] for g in gates),criteria=rule,
        note="Criteria registered before outcomes. Failure retains strong default; no post-hoc threshold changes.")
    atomic_json(root/"晋级判定.json",decision)
    records=[];plans={};prefix_rows=[];wins=[]
    union={(r["case"],int(r["problem"]),int(r["cores"])):r for r in csv.DictReader((HERE/"累计并集/累计1500配置成绩.csv").open(encoding="utf-8-sig"))}
    best={}
    for row in rows:
        s=read_json(row["summary"])
        assert s["logical_calls"]<=cfg["budget"]
        for i,c in enumerate(s["calls"],1):
            r=c["record"]
            records.append(dict(case=row["case"],problem=int(row["problem"]),cores=int(row["cores"]),variant=row["variant"],
                group=row["group"],call=i,name=c["name"],stage=c.get("joint_stage"),phase=c.get("phase"),
                status=r["status"],cache_hit=r.get("cache_hit",False),record=r))
        for cap in cfg["prefixes"]:
            good=[c["record"] for c in s["calls"][:cap] if c["record"]["status"]=="success"]
            r=min(good,key=score) if good else None
            prefix_rows.append(dict(case=row["case"],problem=int(row["problem"]),cores=int(row["cores"]),group=row["group"],variant=row["variant"],
                observed_prefix=cap,calls=min(cap,len(s["calls"])),makespan=score(r)[0] if r else None,
                note="Observed prefix of registered B16 run, not separate B8/B12 rerun"))
        rec=s["best_record"]
        if not rec:continue
        key=row["case"],int(row["problem"]),int(row["cores"])
        if key not in best or score(rec)<score(best[key]["record"]):best[key]=dict(row=row,record=rec)
        plans[f'{row["group"]}/{row["case"]}/p{row["problem"]}_n{row["cores"]}/{row["variant"]}.json']=read_json(rec["plan_path"])
    for key,item in best.items():
        old=union[key];rec=item["record"]
        if score(rec)<(int(old["makespan"]),int(old["added_copy"])):
            wins.append(dict(case=key[0],problem=key[1],cores=key[2],before=int(old["makespan"]),after=score(rec)[0],
                before_copy=int(old["added_copy"]),after_copy=score(rec)[1],
                variant=item["row"]["variant"],record=rec,summary=item["row"]["summary"]))
    atomic_json(root/"待复评新增精选.json",dict(winners=wins))
    write_csv(root/"预算前缀.csv",prefix_rows)
    for name,value in (("全部调用记录.json.gz",records),("各臂最佳方案.json.gz",plans)):
        with gzip.open(root/name,"wt",encoding="utf-8") as f:json.dump(value,f,ensure_ascii=False,separators=(",",":"))
    print(json.dumps(dict(groups=groups,decision=decision,new_cumulative_winners=len(wins),execution=execution),ensure_ascii=False,indent=2))
if __name__=="__main__":main()
