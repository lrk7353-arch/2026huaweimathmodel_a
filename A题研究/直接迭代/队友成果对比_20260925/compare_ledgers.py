"""Compare pinned cumulative ledgers; does not run solvers or certify selected plans."""
import csv, hashlib, json, statistics
from pathlib import Path
HERE = Path(__file__).resolve().parent
DIRECT = HERE.parent
INPUTS = {
    "ours": DIRECT / "全场景架构研究_20260925/批次C_累计精选交付/累计1500配置成绩.csv",
    "team13": HERE / "来源快照/第十三轮成果/全部成绩.csv",
    "common6": HERE / "来源快照/第六轮成果/全部成绩.csv",
}
def read(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    ans = {}
    for r in rows:
        k = (r["case"], int(r["problem"]), int(r["cores"]))
        assert k not in ans, k
        r["time"] = int(r.get("makespan", r.get("after", "")))
        r["copy"] = int(r.get("added_copy", r.get("after_copy_bytes", "")))
        r["base"] = int(r["original_singlecore"])
        assert r["time"] > 0 and r["base"] > 0 and r["copy"] >= 0
        ans[k] = r
    return ans
data = {k: read(v) for k, v in INPUTS.items()}
expected = {(f"case_{i:03d}", p, n) for i in range(1,101) for p in range(1,4) for n in range(1,6)}
assert all(set(d) == expected for d in data.values())
pairs = []
for k in sorted(expected):
    a,b,c = (data[s][k] for s in ("ours","team13","common6"))
    assert a["base"] == b["base"] == c["base"], k
    choice = "ours" if (a["time"],a["copy"]) < (b["time"],b["copy"]) else "team13"
    best = data[choice][k]
    pairs.append(dict(case=k[0],problem=k[1],cores=k[2],original_singlecore=a["base"],
      common6_time=c["time"],ours_time=a["time"],team13_time=b["time"],
      ours_copy=a["copy"],team13_copy=b["copy"],union_time=best["time"],union_copy=best["copy"],
      chosen=choice,time_winner="tie" if a["time"]==b["time"] else choice,
      common6_speedup=c["base"]/c["time"],ours_speedup=a["base"]/a["time"],
      team13_speedup=b["base"]/b["time"],union_speedup=best["base"]/best["time"],
      union_vs_team_pct=100*(1-best["time"]/b["time"]),
      team_vs_ours_pct=100*(1-b["time"]/a["time"]),
      ours_plan=a["plan"],ours_plan_base=a.get("plan_base",""),team13_plan=b["plan"],
      ours_source=a.get("source",""),status="ledger comparison; union not independently replayed"))
groups=[]
for p in range(1,4):
  for n in range(1,6):
    rs=[r for r in pairs if (r["problem"],r["cores"])==(p,n)]
    groups.append(dict(problem=p,cores=n,cases=len(rs),
      **{name+"_mean_speedup":statistics.mean(r[name+"_speedup"] for r in rs) for name in ("common6","ours","team13","union")},
      ours_faster=sum(r["ours_time"]<r["team13_time"] for r in rs),
      team_faster=sum(r["team13_time"]<r["ours_time"] for r in rs),
      time_ties=sum(r["ours_time"]==r["team13_time"] for r in rs),
      ours_tie_less_copy=sum(r["ours_time"]==r["team13_time"] and r["ours_copy"]<r["team13_copy"] for r in rs),
      team_tie_less_copy=sum(r["ours_time"]==r["team13_time"] and r["ours_copy"]>r["team13_copy"] for r in rs),
      union_vs_team_mean_time_reduction_pct=statistics.mean(r["union_vs_team_pct"] for r in rs)))
def write(name,rs):
    with (HERE/name).open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(rs[0]));w.writeheader();w.writerows(rs)
write("逐配置对账.csv",pairs);write("各核数对比.csv",groups)
prov=[dict(name=k,path=str(v.relative_to(DIRECT)),sha256=hashlib.sha256(v.read_bytes()).hexdigest()) for k,v in INPUTS.items()]
write("输入文件校验.csv",prov)
print(json.dumps({"groups":groups,"all_counts":{s:sum(r["time_winner"]==s for r in pairs) for s in ("ours","team13","tie")},
 "ours_tie_less_copy":sum(r["ours_time"]==r["team13_time"] and r["ours_copy"]<r["team13_copy"] for r in pairs),
 "team_tie_less_copy":sum(r["ours_time"]==r["team13_time"] and r["ours_copy"]>r["team13_copy"] for r in pairs),
 "top_ours":sorted([r for r in pairs if r["cores"]==5], key=lambda r:r["union_vs_team_pct"],reverse=True)[:12],
 "top_team":sorted([r for r in pairs if r["cores"]==5],key=lambda r:r["team_vs_ours_pct"],reverse=True)[:12]},ensure_ascii=False,indent=2))
