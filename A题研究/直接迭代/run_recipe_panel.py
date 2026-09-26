"""Frozen, paired cold-start comparison for P23 recipe admission."""
import argparse
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import json
from common_run import atomic_json, score
from frontier_solver import run

CASES = (19,35,49,50,66,1,9,23,25,28,37,46,53,71,95)


def pair(job):
    index, case, problem, root = job
    out = Path(root)/f"case_{case:03d}"/f"p{problem}_n5"
    policies = ("legacy", "paired_w200") if index % 2 == 0 else ("paired_w200", "legacy")
    results = {}
    for policy in policies:
        s = run(f"case_{case:03d}",problem,5,out/policy,24,240,60,"frontier",policy)
        results[policy] = dict(summary=str(out/policy/"summary.json"),
            score=score(s["best_record"]) if s["best_record"] else None,
            calls=s["logical_calls"], failed=sum(c["record"]["status"]!="success" for c in s["calls"]),
            seconds=s["elapsed_seconds"])
    return dict(case=case,problem=problem,cores=5,order=policies,arms=results)


if __name__=="__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out",type=Path,required=True)
    a = p.parse_args()
    a.out.mkdir(parents=True,exist_ok=False)
    jobs=[(i,c,pr,str(a.out.resolve())) for i,(c,pr) in
          enumerate((c,pr) for c in CASES for pr in (2,3))]
    atomic_json(a.out/"protocol.json",dict(cases=CASES,cores=5,problems=[2,3],budget=24,
        seconds=240,single_timeout=60,workers=2,arms=["legacy","paired_w200"],
        order="alternate within configuration; two independent configurations concurrent",
        scope="development and regression; not blind; all starts from graph, no selected-library plans"))
    results=[]
    with ProcessPoolExecutor(max_workers=2) as pool:
        for r in pool.map(pair,jobs):
            results.append(r);atomic_json(a.out/"results.json",results)
            print(json.dumps(r),flush=True)
