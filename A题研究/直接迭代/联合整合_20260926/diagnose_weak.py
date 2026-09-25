"""Four fresh diagnostics of weak P1 plans; no tuning or search."""
import csv,gzip,json,sys,time
from pathlib import Path
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parent))
from common_run import DATA,GraphIR,run_candidate,read_json,atomic_json,score,write_csv
from p1_bottleneck_diagnose import diagnose
from p1_boundary_lower_bound import boundary_ddr_lower_bound
from p1_selective import task_lower_bound
from analyze_wcc_transition import merged,intersection

def main():
    out=HERE/"薄弱图诊断";out.mkdir(exist_ok=False)
    rows=[];records=[]
    for i in (64,44,69,47):
        case=f"case_{i:03d}";plan=read_json(HERE/f"累计并集/方案/p1/n5/{case}_multicore_res.json")
        r=run_candidate(case,1,5,plan,out/"复评运行"/case,timeout=180)
        records.append(r);assert r["status"]=="success",r["status"]
        ir=GraphIR.from_path(DATA/(case+".json"))
        with gzip.open(r["result_path"],"rt") as f:raw=json.load(f)
        d=diagnose(ir,plan,raw);bound=boundary_ddr_lower_bound(ir,plan);task=task_lower_bound(ir,plan)
        per=[]
        for core in raw["per_core_timeline"]:
            values={}
            for pipe in ("PIPE_M","PIPE_V"):
                intervals=merged([(op["start"],op["end"]) for op in core["ops"] if op["op_id"] in ir.compute_ids and op["pipe"]==pipe])
                values[pipe]=sum(b-a for a,b in intervals)
            per.append(max(values.values()))
        row=dict(case=case,makespan=score(r)[0],tasks=d["task_count"],critical_tasks=len(d["final_blocker_chain"]),
            largest_task_duration=d["heavy_tasks"][0]["duration"],largest_task_ops=d["heavy_tasks"][0]["ops"],
            max_core_pipe_busy=max(per),max_pipe_fraction=max(per)/score(r)[0],
            original_compute_path=d["compute_dependency_path"],compute_path_fraction=d["compute_dependency_path"]/score(r)[0],
            boundary_ddr_bound=bound["lower_bound"],boundary_ddr_fraction=bound["lower_bound"]/score(r)[0],
            fixed_task_bound=task["value"],spill_bytes=r["metrics"]["data_movement_bytes"]["spill_added_copy_bytes"])
        rows.append(row);write_csv(out/"诊断摘要.csv",rows)
        atomic_json(out/(case+".json"),dict(summary=row,diagnosis=d,ddr=bound,task_bound=task,record=r))
        print(json.dumps(row),flush=True)
    atomic_json(out/"复评账本.json",dict(calls=4,records=records,scope="diagnostic replay only; no candidate search"))
if __name__=="__main__":main()
