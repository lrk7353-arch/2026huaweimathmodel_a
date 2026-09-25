"""Replay four pinned teammate plans, never search new candidates."""
import csv, json, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent))
from common_run import run_candidate, score, read_json
def main():
    out = HERE / "复评运行"
    if out.exists():
        raise RuntimeError("Use a fresh replay directory; existing evidence is retained.")
    ledger = {(r["case"],int(r["problem"]),int(r["cores"])):r for r in csv.DictReader((HERE/"来源快照/第十三轮成果/全部成绩.csv").open(encoding="utf-8-sig"))}
    records=[]
    for case,p,n in [("case_085",1,5),("case_095",3,5),("case_046",3,5),("case_040",1,5)]:
        row=ledger[case,p,n]
        plan_path=HERE/"来源快照/第十三轮成果"/row["plan"]
        rec=run_candidate(case,p,n,read_json(plan_path),out/(case+f"_p{p}"),timeout=45)
        ok=rec["status"]=="success"
        actual=score(rec) if ok else (None,None)
        entry=dict(case=case,problem=p,cores=n,status=rec["status"],expected_time=int(row["after"]),actual_time=actual[0],expected_copy=int(row["after_copy_bytes"]),actual_copy=actual[1],matches=ok and actual==(int(row["after"]),int(row["after_copy_bytes"])),cache_hit=rec["cache_hit"],elapsed_seconds=rec["elapsed_seconds"],record_path=rec["record_path"])
        records.append(entry)
        with (HERE/"独立抽查复评.csv").open("w",encoding="utf-8-sig",newline="") as f:
            w=csv.DictWriter(f,fieldnames=list(entry));w.writeheader();w.writerows(records)
        print(json.dumps(entry,ensure_ascii=False),flush=True)
    assert all(r["matches"] and not r["cache_hit"] for r in records)
if __name__=="__main__":main()
