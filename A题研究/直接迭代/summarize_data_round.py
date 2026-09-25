"""Round-11 comparisons with fixed inputs, separate from adaptive portfolio."""
import argparse
import statistics
from common_run import *


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--panels',nargs='+',required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True);rows=[];costs=[];comparisons=[];accepted=[]
    for arg in a.panels:
        group,path=arg.split('=',1);panel=read_json(Path(path)/'summary.json');group_rows=[]
        for row in panel['rows']:
            if row['status']!='success':raise ValueError('failed arm in panel')
            s=read_json(row['summary_path']);r=dict(group=group,**row,reduction_pct=100*(1-s['after']/s['before']))
            group_rows.append(r);rows.append(r)
            for i,c in enumerate(s['calls'],1):
                if c['accepted']:
                    b=read_json(c['parent_record']);q=c['record'];bm=b['metrics']['data_movement_bytes'];qm=q['metrics']['data_movement_bytes']
                    accepted.append(dict(group=group,case=s['case'],problem=s['problem'],cores=s['num_cores'],policy=s['policy'],call=i,
                        family=c['metadata'].get('family'),name=c['name'],parent_time=score(b)[0],time=score(q)[0],
                        parent_spill=bm['spill_added_copy_bytes'],spill=qm['spill_added_copy_bytes'],
                        parent_copy=bm['scheduled_copy_bytes'],copy=qm['scheduled_copy_bytes'],record=q['record_path']))
        costs.append(dict(group=group,wall_seconds=panel['wall_seconds'],arms=len(group_rows),
            logical_calls=sum(r['logical_calls'] for r in group_rows),new_calls=sum(r['new_calls'] for r in group_rows),
            failed_calls=sum(r['failed_calls'] for r in group_rows),generation_seconds=sum(r['generation_seconds'] for r in group_rows)))
        by={(r['case'],r['problem'],r['num_cores'],r['policy']):r for r in group_rows}
        for problem in (2,3):
            for method,control in (('data','legacy'),('data','region'),('region','legacy')):
                pairs=[(r,by[c,p,n,control]) for (c,p,n,m),r in by.items() if p==problem and m==method and (c,p,n,control) in by]
                if not pairs:continue
                if any(r['before']!=b['before'] for r,b in pairs):raise ValueError('comparison input differs')
                gains=[100*(1-r['after']/b['after']) for r,b in pairs]
                comparisons.append(dict(group=group,problem=problem,method=method,control=control,pairs=len(pairs),
                    wins=sum(r['after']<b['after'] for r,b in pairs),ties=sum(r['after']==b['after'] for r,b in pairs),
                    losses=sum(r['after']>b['after'] for r,b in pairs),mean_paired_reduction_pct=statistics.mean(gains),
                    median_paired_reduction_pct=statistics.median(gains),mean_reduction_from_input_pct=statistics.mean(r['reduction_pct'] for r,b in pairs)))
    write_csv(a.out/'方法逐图比较.csv',rows);write_csv(a.out/'方法汇总比较.csv',comparisons)
    write_csv(a.out/'实验成本.csv',costs);write_csv(a.out/'接受候选明细.csv',accepted)
    atomic_json(a.out/'方法分析.json',dict(comparisons=comparisons,costs=costs,
        scope='warm-start additional budget; development and validation fixed inputs; adaptive continuation is not same-budget ranking'))
    print(json.dumps(dict(comparisons=comparisons,costs=costs),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
