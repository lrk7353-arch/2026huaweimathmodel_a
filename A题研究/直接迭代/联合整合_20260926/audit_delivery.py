"""Check the frozen source, official input, and portable final delivery."""
import csv,gzip,hashlib,json,math,statistics
from collections import Counter
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
def read_csv(path):
    with path.open(encoding="utf-8-sig") as f:return list(csv.DictReader(f))
def main():
    manifest=json.loads((HERE/"同预算对照/manifest.json").read_text())
    for path,digest in manifest["source_hashes"].items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    official=read_csv(HERE.parent/"队友成果对比_20260925/官方输入一致性检查.csv")
    for row in official:
        raw=(ROOT/row["path"]).read_bytes()
        digest=hashlib.sha1(b"blob "+str(len(raw)).encode()+b"\0"+raw).hexdigest()
        assert digest==row["local_blob"]==row["teammate_blob"],row["path"]
    out=HERE/"最终累计";rows=read_csv(out/"累计1500配置成绩.csv")
    expected={(f"case_{i:03d}",p,n) for i in range(1,101) for p in (1,2,3) for n in range(1,6)}
    assert len(rows)==1500 and {(r["case"],int(r["problem"]),int(r["cores"])) for r in rows}==expected
    old={(r["case"],r["problem"],r["cores"]):r for r in read_csv(HERE/"累计并集/累计1500配置成绩.csv")}
    improved=Counter()
    for row in rows:
        raw=(out/row["plan"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==row["plan_sha256"],row["plan"]
        assert math.isclose(float(row["speedup"]),int(row["original_singlecore"])/int(row["makespan"]),rel_tol=1e-12)
        prev=old[row["case"],row["problem"],row["cores"]]
        score=lambda r:(int(r["makespan"]),int(r["added_copy"]))
        assert score(row)<=score(prev)
        if score(row)<score(prev):improved[row["problem"]]+=1
    bundles=0
    for p in (1,2,3):
        for n in range(1,6):
            with gzip.open(out/f"方案分包/p{p}_n{n}.json.gz","rt",encoding="utf-8") as f:bundle=json.load(f)
            subset=[r for r in rows if (int(r["problem"]),int(r["cores"]))==(p,n)]
            assert len(bundle)==100 and set(bundle)=={r["plan"] for r in subset}
            for r in subset:
                raw=json.dumps(bundle[r["plan"]],ensure_ascii=False,separators=(",",":")).encode()
                assert hashlib.sha256(raw).hexdigest()==r["plan_sha256"],r["plan"]
            bundles+=1
    replay=json.loads((HERE/"新增精选复评/结果.json").read_text())
    assert replay["attempted"]==replay["passed"]==sum(improved.values())==52
    pairs=read_csv(HERE/"同预算对照/逐配置比较.csv")
    additional=[]
    for p in (1,2,3):
        rs=[r for r in pairs if int(r["problem"])==p]
        val=[r for r in rs if r["group"]=="validation"]
        additional.append(dict(problem=p,total_configs=len(rs),joint_wins=sum(r["winner"]=="joint" for r in rs),
          ties=sum(r["winner"]=="tie" for r in rs),strong_wins=sum(r["winner"]=="strong" for r in rs),
          validation_wins_over_half_percent=sum(float(r["reduction_pct"])>=.5 for r in val),
          all_groups_mean_paired_reduction_pct=statistics.mean(float(r["reduction_pct"]) for r in rs),
          aggregate_solver_wall_increase_pct=100*(sum(float(r["joint_seconds"]) for r in rs)/sum(float(r["strong_seconds"]) for r in rs)-1)))
    with gzip.open(HERE/"同预算对照/全部调用记录.json.gz","rt",encoding="utf-8") as f:calls=json.load(f)
    assert len(calls)==3376 and not any(r["cache_hit"] for r in calls)
    result=dict(frozen_source_files=len(manifest["source_hashes"]),official_files=len(official),
      plans=len(rows),bundles=bundles,independent_new_winner_replays=replay["passed"],
      strict_improvements_by_problem=dict(improved),call_statuses=dict(Counter(r["status"] for r in calls)),
      supplemental_analysis=additional,passed=True)
    (HERE/"交付核验.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
