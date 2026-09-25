"""Isolate calendar gap insertion from the same ordering/placement machinery."""
import argparse
import csv
from pathlib import Path
import time

from common_run import DATA, GraphIR, atomic_json, evaluate, read_json, score
import event_frontier as frontier
from unified_structure import Structure

HERE=Path(__file__).resolve().parent
LEDGER=HERE/'三指标联合推进_20260926/累计1500配置成绩.csv'


class AppendCalendar(frontier.Calendar):
    def slot(self, release, duration):
        return max(release,self.ends[-1] if self.ends else 0),len(self.intervals)


def main():
    p=argparse.ArgumentParser();p.add_argument('--cases',default='9,23,53')
    p.add_argument('--out',type=Path,required=True);args=p.parse_args()
    args.out.mkdir(parents=True,exist_ok=False)
    chosen={f'case_{int(x):03d}' for x in args.cases.split(',')}
    rows=[r for r in csv.DictReader(LEDGER.open(encoding='utf-8-sig'))
          if r['case'] in chosen and r['problem'] in ('2','3') and r['cores']=='5']
    original_calendar=frontier.Calendar;results=[]
    for row in rows:
        ir=GraphIR.from_path(DATA/(row['case']+'.json'));structure=Structure(ir)
        parent=read_json(LEDGER.parent/row['plan'])
        core_by_task={t:c for c,seq in enumerate(parent['core_schedules']) for t in seq}
        fixed={int(o):core_by_task[t] for o,t in parent['node_to_subgraph'].items()}
        item=dict(case=row['case'],problem=int(row['problem']),baseline=int(row['makespan']),controls={})
        for mode,calendar in [('append',AppendCalendar),('insert',original_calendar)]:
            # Only calendar availability changes; ready order, ownership,
            # communication proxy and plan encoder remain identical.
            frontier.Calendar=calendar
            owner,order,meta=frontier.insertion_schedule(structure,5,fixed=fixed)
            plan=frontier.operation_plan(ir,order,owner,5)
            record=evaluate(ir.path,plan,int(row['problem']),args.out/'evaluations',timeout=100,config_path=DATA/'config.txt')
            item['controls'][mode]=dict(record=record,metadata=meta)
        results.append(item);atomic_json(args.out/'results.json',results)
        print(row['case'],row['problem'],row['makespan'],
              {k:score(v['record'])[0] if v['record']['status']=='success' else v['record']['status']
               for k,v in item['controls'].items()},flush=True)
    frontier.Calendar=original_calendar


if __name__=='__main__':main()
