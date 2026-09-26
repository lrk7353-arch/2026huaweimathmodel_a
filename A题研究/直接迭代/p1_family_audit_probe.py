"""Bounded warm diagnostic: change partition family, keep the region fixed.

Ten fresh official calls maximum: two paid parent replays and eight proposals.
No change to production source, exported best plans, or official evaluators.
"""
import gzip
import json
from pathlib import Path
import time
from unittest.mock import patch

from common_run import DATA, GraphIR, evaluate, write_csv
from p1_latest_audit import plans, RELEASE, OUT
from p1_recipe_bootstrap import generation_limit
import persistent_p1_moves as moves
from wait_observation import observe


def main():
    parents = plans(RELEASE/'从头完整1500/plans.tar.gz')
    out = OUT/'探针运行'
    out.mkdir(parents=True, exist_ok=False)
    rows = []
    original_partition = moves.partition_region
    for case in ('case_043', 'case_085'):
        case_start = time.monotonic()
        ir = GraphIR.from_path(DATA/(case+'.json'))
        plan = parents[case]
        parent = evaluate(ir.path, plan, 1, out/case/'parent', timeout=60, config_path=DATA/'config.txt')
        (out/case/'parent.json').write_text(json.dumps(parent, ensure_ascii=False, indent=2))
        if parent['status'] != 'success':
            print(case, 'parent failed', flush=True)
            continue
        with gzip.open(parent['result_path'], 'rt') as f:
            raw=json.load(f)
        observation=None
        try:
            with generation_limit(12):
                observation=observe(ir,plan,raw,1)
        except TimeoutError:
            pass
        for family in ('affinity','branch'):
            def partition(ir, members, target, _family, order, affinity, deadline):
                return original_partition(ir,members,target,family,order,affinity,deadline)
            with patch.object(moves,'partition_region',side_effect=partition):
                stream=moves.iter_candidates(ir,plan,raw,5,round_index=0,observation=observation,seconds=24)
                for _ in range(4):
                    try:
                        with generation_limit(12):
                            proposal=next(stream)
                    except StopIteration:
                        break
                    if proposal['name'].endswith('_merge'):
                        continue
                    remaining=240-(time.monotonic()-case_start)
                    if remaining<=0:
                        break
                    record=evaluate(ir.path,proposal['plan'],1,out/case/family,timeout=min(60,remaining),config_path=DATA/'config.txt')
                    metric=record.get('metrics',{})
                    row=dict(case=case,family=family,cap=proposal['metadata']['region']['cap'],status=record['status'],
                             parent_cycles=raw['makespan'],cycles=metric.get('makespan'),
                             added_copy=metric.get('data_movement_bytes',{}).get('added_copy_bytes'),
                             observation=proposal['metadata']['observation_source'], record_path=record.get('record_path'))
                    rows.append(row)
                    write_csv(OUT/'固定区域换重划族探针.csv',rows)
                    print(json.dumps(row,ensure_ascii=False),flush=True)
                stream.close()
    print('Complete; warm family-isolation diagnostic, not cold B24 evidence.',flush=True)


if __name__=='__main__':
    main()
