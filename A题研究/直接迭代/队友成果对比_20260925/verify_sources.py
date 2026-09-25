import csv, json, statistics, hashlib
from pathlib import Path
H=Path(__file__).resolve().parent
R=H.parents[2]
def gh(p):
    x=p.read_bytes()
    return hashlib.sha1(b"blob "+str(len(x)).encode()+b"\0"+x).hexdigest()
checks=[]
for row in csv.DictReader((H/"官方输入远端版本.csv").open()):
    actual=gh(R/row["path"])
    checks.append(dict(path=row["path"],teammate_blob=row["sha"],local_blob=actual,matches=actual==row["sha"]))
assert all(r["matches"] for r in checks)
with (H/"官方输入一致性检查.csv").open("w",encoding="utf-8-sig",newline="") as f:
    w=csv.DictWriter(f,fieldnames=list(checks[0]));w.writeheader();w.writerows(checks)
ps=list(csv.DictReader((H/"逐配置对账.csv").open(encoding="utf-8-sig")))
transfer=[r for r in ps if r["chosen"]=="ours"]
with (H/"建议纳入队友库的111项.csv").open("w",encoding="utf-8-sig",newline="") as f:
    w=csv.DictWriter(f,fieldnames=list(transfer[0]));w.writeheader();w.writerows(transfer)
print("official files matched:",len(checks))
print("teammate strict vs common6:",sum(int(r["team13_time"])<int(r["common6_time"]) for r in ps))
for p in (1,2,3):
    rs=[r for r in ps if int(r["problem"])==p and int(r["cores"])==5]
    print("P",p,"ours top",[(r["case"],r["ours_time"],r["team13_time"],round(float(r["union_vs_team_pct"]),3)) for r in sorted(rs,key=lambda x:float(x["union_vs_team_pct"]),reverse=True)[:5]])
    print("team top",[(r["case"],r["ours_time"],r["team13_time"],round(float(r["team_vs_ours_pct"]),3)) for r in sorted(rs,key=lambda x:float(x["team_vs_ours_pct"]),reverse=True)[:5]])
    print("weak union",[(r["case"],round(float(r["union_speedup"]),3),r["chosen"]) for r in sorted(rs,key=lambda x:float(x["union_speedup"]))[:8]])
for source in ("ours","team13","union"):
    d={(r["case"],int(r["problem"])):int(r[source+"_time"]) for r in ps if int(r["cores"])==5}
    print(source,"P2/P3",statistics.mean(d[(f"case_{i:03d}",2)]/d[(f"case_{i:03d}",3)] for i in range(1,101)))
