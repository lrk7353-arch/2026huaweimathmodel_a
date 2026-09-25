"""Audit charged parent provenance and realized neighborhood gains in a panel."""
import argparse
from collections import defaultdict
from common_run import *


def analyze(folder):
    folder=Path(folder);s=read_json(folder/'summary.json')
    if not s['complete']:raise ValueError('incomplete panel')
    events=[];families=defaultdict(lambda:dict(calls=0,accepted=0,time_improvements=0,alternative_parent_calls=0,
        alternative_parent_time_improvements=0,strictly_worse_parent_calls=0,generation_seconds=0.))
    for row in s['rows']:
        summary=read_json(row['summary_path']);best=None;charged={}
        for i,c in enumerate(summary.get('calls',summary.get('evaluations',[])),1):
            rec=c['record'];parent=c.get('parent_record');phase=c.get('phase','unknown')
            alternate=bool(parent and best and parent!=best['record_path'])
            if parent and parent not in charged:raise ValueError('parent was not charged before this candidate')
            worse=bool(parent and best and score(charged[parent])[0]>score(best)[0])
            improved=rec['status']=='success' and best is not None and score(rec)[0]<score(best)[0]
            if c.get('stage')=='continuation':
                group=families[row['problem'],row['method'],phase]
                group['calls']+=1;group['accepted']+=bool(c['accepted']);group['time_improvements']+=improved
                group['alternative_parent_calls']+=alternate;group['alternative_parent_time_improvements']+=alternate and improved
                group['strictly_worse_parent_calls']+=worse
                events.append(dict(case=row['case'],problem=row['problem'],method=row['method'],call=i,family=phase,
                    parent_record=parent,parent_span=score(charged[parent])[0] if parent else None,
                    best_before=score(best)[0] if best else None,alternative_parent=alternate,
                    strictly_worse_parent=worse,status=rec['status'],time_improved=improved,
                    span=score(rec)[0] if rec['status']=='success' else None))
            if rec['status']=='success':
                charged[rec['record_path']]=rec
                if best is None or score(rec)<score(best):best=rec
        if best is not None and score(best)!=score(summary['best_record']):raise ValueError('uncharged final score')
        for stage in summary.get('stages',[]):
            if stage.get('phase')=='local_generation':
                families[row['problem'],row['method'],stage['family']]['generation_seconds']+=stage['seconds']
    rows=[dict(problem=p,method=m,family=f,**v) for (p,m,f),v in sorted(families.items())]
    write_csv(folder/'parent_events.csv',events);write_csv(folder/'family_effects.csv',rows)
    atomic_json(folder/'parent_audit.json',dict(complete=True,rows=rows,continuation_events=len(events),
        scope='all referenced parents come from earlier charged successes in the same run; counts are path-dependent, not isolated causal effects'))
    return rows


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--panel',required=True);a=p.parse_args()
    print(json.dumps(analyze(a.panel),ensure_ascii=False,indent=2))
