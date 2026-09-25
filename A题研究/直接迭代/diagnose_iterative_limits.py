"""Post-hoc diagnostic: compose rejected splits with Task reassignment.

Reuses existing split traces to test a mechanism, NOT an equal-budget ranking.
Writes fresh official calls to a new run directory and compact public evidence.
"""
from concurrent.futures import ThreadPoolExecutor
import gzip
from common_run import *
from p1_task_refine import reschedule

ROOT = R.parent
OUT = ROOT/'my_runs/split_reassign_diagnostic_v1'
EXPORT = R/'直接迭代/闭环验证_20260925/瓶颈复核'


def one(item):
    ev, metric = item
    rec = ev['record']
    ir = GraphIR.from_path(DATA/'case_043.json')
    with gzip.open(rec['result_path'], 'rt') as f:
        raw = json.load(f)
    plan = reschedule(ir, read_json(rec['plan_path']), raw, metric, seed=17)
    name = ev['name']+'_'+metric
    result = run_candidate('case_043', 1, 4, plan, OUT/name, timeout=60)
    row = dict(case='case_043', cores=4, candidate=name,
        original_parent=266766, split_intermediate=score(rec)[0],
        after=score(result)[0] if result['status']=='success' else None,
        added_copy=score(result)[1] if result['status']=='success' else None,
        status=result['status'], fresh_call=not result['cache_hit'],
        previous_cold_best=243378,
        beats_parent=result['status']=='success' and score(result)[0]<266766,
        beats_cold_best=result['status']=='success' and score(result)[0]<243378,
        record=str(Path(result['record_path']).relative_to(ROOT)))
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return row


if __name__=='__main__':
    OUT.mkdir(parents=True, exist_ok=False)
    source=ROOT/'my_runs/iterative_holdout_b12_v1/case_043/n4/iterative/summary.json'
    splits=[x for x in read_json(source)['evaluations'] if x['name'].startswith('split')]
    assert len(splits)==2 and all(not x['accepted'] for x in splits)
    items=[(x,metric) for x in splits for metric in ('local','observed')]
    atomic_json(OUT/'protocol.json', dict(scope='post-hoc mechanism probe; prior trace costs not included; not uniform budget',
        source=str(source.relative_to(ROOT)), candidates=[x['name']+'_'+m for x,m in items],
        workers=2, calls=4, timeout_seconds=60, seed=17))
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows=list(pool.map(one,items))
    write_csv(EXPORT/'拆分后重分配.csv',rows)
    atomic_json(OUT/'summary.json',dict(completed=True,rows=rows))
