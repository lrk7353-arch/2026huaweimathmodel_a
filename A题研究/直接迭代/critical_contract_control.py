"""Keep original P1 blocks; isolate the effect of contraction from reassignment."""
import argparse
from collections import defaultdict
import csv
import time
from pathlib import Path

from common_run import DATA,GraphIR,read_json,atomic_json,evaluate,score
from p1_selective import _toposort_blocks,block_views,_assign
from unified_structure import Structure


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=False)
    ledger=Path(__file__).with_name('三指标联合推进_20260926')/'累计1500配置成绩.csv'
    selected={'case_005','case_047','case_069','case_071','case_086'}
    rows=[r for r in csv.DictReader(ledger.open(encoding='utf-8-sig'))
          if r['case'] in selected and r['problem']=='1' and r['cores']=='5']
    for row in rows:
        start=time.monotonic();ir=GraphIR.from_path(DATA/(row['case']+'.json'))
        parent=read_json(ledger.parent/row['plan']);structure=Structure(ir)
        blocks=defaultdict(list)
        for o in structure.topo:blocks[parent['node_to_subgraph'][str(o)]].append(o)
        blocks=_toposort_blocks(ir,list(blocks.values()),structure.topo)
        plan,proxy=_assign(ir,blocks,block_views(ir,blocks),5,5,'eft')
        generation=time.monotonic()-start
        record=evaluate(ir.path,plan,1,a.out/row['case']/'evaluations',timeout=60,config_path=DATA/'config.txt')
        result=dict(case=row['case'],problem=1,cores=5,complete=True,budget=1,
                    ledger_baseline=int(row['makespan']),best_record=record if record['status']=='success' else None,
                    generation_seconds=generation,generation_errors=[],elapsed_seconds=time.monotonic()-start,
                    calls=[dict(name='unchanged_partition_reassign',record=record,metadata=dict(task_proxy=proxy))],
                    scope='one added attribution call; baseline already paid in critical-contract experiment')
        atomic_json(a.out/row['case']/'p1/summary.json',result)
        print(row['case'],record['status'],score(record) if record['status']=='success' else None,flush=True)
