"""Audit and archive a completed paired panel, including paid COPY tradeoffs."""
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
from statistics import mean
import tarfile

from common_run import read_json, atomic_json, write_csv, score

ROOT=Path(__file__).resolve().parent
OUT=ROOT/"统一算法再攻坚研究_20260926/同预算端到端30配置"
SOURCE=ROOT/"运行结果/再攻坚P23同预算30配置_v1"
DEV={19,35,49,50,66}


def main():
    pairs=read_json(SOURCE/"results.json")
    assert len(pairs)==30
    assert {(r["case"],r["problem"]) for r in pairs}=={
        (c,p) for c in (19,35,49,50,66,1,9,23,25,28,37,46,53,71,95) for p in (2,3)}
    OUT.mkdir(parents=True,exist_ok=True)
    ledger={r["case"]:float(r["original_singlecore"]) for r in csv.DictReader(
        (ROOT/"真机启发攻坚_20260926/从头完整1500/从头1500配置成绩.csv").open(encoding="utf-8-sig"))}
    rows,alternatives,summaries,plans=[],[],[],{}
    for pair in pairs:
        c,p=pair["case"],pair["problem"]
        row=dict(case=f"case_{c:03d}",problem=p,cores=5,group="development" if c in DEV else "regression")
        for policy in ("legacy","paired_w200"):
            s=read_json(pair["arms"][policy]["summary"])
            assert s["complete"] and s["logical_calls"]==len(s["calls"])<=24
            assert s["best_record"] and s["best_record"]["status"]=="success"
            prefix=read_json(Path(pair["arms"][policy]["summary"]).parent/"mature_prefix/summary.json")
            summaries.append(dict(case=c,problem=p,policy=policy,summary=s,prefix_stages=prefix["stages"]))
            paid=[x["record"] for x in s["calls"] if x["record"]["status"]=="success"]
            b=s["best_record"];t,copy=score(b)
            assert (t,copy)==min(map(score,paid))
            row.update({policy+"_makespan":t,policy+"_speedup":ledger[row["case"]]/t,
                        policy+"_copy":copy,policy+"_calls":len(s["calls"]),
                        policy+"_failures":len(s["calls"])-len(paid),
                        policy+"_seconds":s["elapsed_seconds"],
                        policy+"_logged_generation_seconds":s["generation_seconds"]+sum(
                            st.get("generation_seconds",0) for st in prefix["stages"])})
            cache=b["metrics"].get("cache_stats",{})
            row[policy+"_byte_hit_rate"]=cache.get("hit_bytes",0)/max(1,cache.get("hit_bytes",0)+cache.get("miss_bytes",0)) if p==3 else ""
            for allowance in (0,.01,.03):
                chosen=min((r for r in paid if score(r)[0]<=t*(1+allowance)),
                           key=lambda r:(score(r)[1],score(r)[0]))
                data=Path(chosen["plan_path"]).read_bytes()
                h=hashlib.sha256(data).hexdigest();assert h==chosen["hashes"]["plan_sha256"]
                plans[h]=data
                alternatives.append(dict(case=row["case"],problem=p,cores=5,policy=policy,
                    allowed_time_regression=allowance,makespan=score(chosen)[0],
                    added_copy=score(chosen)[1],actual_time_regression=score(chosen)[0]/t-1,
                    bytes_saved=copy-score(chosen)[1],plan_member=f"plans/{h}.json"))
        row["time_reduction"]=1-row["paired_w200_makespan"]/row["legacy_makespan"]
        row["copy_delta"]=row["paired_w200_copy"]-row["legacy_copy"]
        row["outcome"]="win" if row["time_reduction"]>0 else "loss" if row["time_reduction"]<0 else "tie"
        old_pair=(row['legacy_makespan'],row['legacy_copy'])
        new_pair=(row['paired_w200_makespan'],row['paired_w200_copy'])
        row['lexicographic_outcome']='win' if new_pair<old_pair else 'loss' if new_pair>old_pair else 'tie'
        rows.append(row)
    aggregates=[]
    for p in (2,3):
        for group in ("all","development","regression"):
            rs=[r for r in rows if r["problem"]==p and (group=="all" or r["group"]==group)]
            record=dict(problem=p,group=group,count=len(rs),
                wins=sum(r["outcome"]=="win" for r in rs),
                ties=sum(r["outcome"]=="tie" for r in rs),
                losses=sum(r["outcome"]=="loss" for r in rs))
            record['time_tie_copy_regressions']=sum(r['outcome']=='tie' and r['copy_delta']>0 for r in rs)
            record['copy_increase_cases']=sum(r['copy_delta']>0 for r in rs)
            for policy in ("legacy","paired_w200"):
                record[policy+"_mean_speedup"]=mean(r[policy+"_speedup"] for r in rs)
                for name in ("copy","calls","failures","seconds","logged_generation_seconds"):
                    record[policy+"_"+name]=sum(r[policy+"_"+name] for r in rs)
            aggregates.append(record)
    write_csv(OUT/"逐图对照.csv",rows)
    write_csv(OUT/"同付费候选池搬运折中.csv",alternatives)
    atomic_json(OUT/"汇总.json",aggregates)
    atomic_json(OUT/"protocol.json",read_json(SOURCE/"protocol.json"))
    with gzip.open(OUT/"全部从头调用与生成阶段.json.gz","wt",encoding="utf-8") as f:
        json.dump(summaries,f,ensure_ascii=False,separators=(",",":"))
    with tarfile.open(OUT/"最优与折中方案.tar.gz","w:gz") as tar:
        for h,data in plans.items():
            info=tarfile.TarInfo(f"plans/{h}.json");info.size=len(data)
            tar.addfile(info,io.BytesIO(data))
    print(json.dumps(aggregates,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
