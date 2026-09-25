"""Separate warm-start algorithm comparisons from accumulated export gains."""
import argparse
import statistics
from common_run import *


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--panels',nargs='+',required=True,help='group=panel_path')
    p.add_argument('--out',required=True,type=Path);a=p.parse_args();a.out.mkdir(exist_ok=True,parents=True)
    all_rows=[];costs=[];comparisons=[];curves=[]
    for argument in a.panels:
        group,path=argument.split('=',1);root=Path(path);panel=read_json(root/'summary.json');rows=[]
        for row in panel['rows']:
            if row['status']!='success':
                rows.append(dict(group=group,**row));continue
            s=read_json(row['summary_path']);curve=[];best=s['before']
            for i,c in enumerate(s['calls'],1):
                if c['record']['status']=='success':best=min(best,score(c['record'])[0])
                curve.append(best)
            rows.append(dict(group=group,**row,reduction_pct=100*(1-s['after']/s['before'])))
            for cap in (12,24,48):
                if cap>s['budget']:continue
                after=curve[min(cap,len(curve))-1] if curve else s['before']
                curves.append(dict(group=group,case=s['case'],problem=s['problem'],cores=s['num_cores'],policy=s['policy'],
                    cap=cap,calls=min(cap,len(curve)),before=s['before'],after=after,reduction_pct=100*(1-after/s['before'])))
        all_rows+=rows
        costs.append(dict(group=group,wall_seconds=panel['wall_seconds'],configs=len(rows),
            logical_calls=sum(r.get('logical_calls',0) for r in rows),new_calls=sum(r.get('new_calls',0) for r in rows),
            failed_calls=sum(r.get('failed_calls',0) for r in rows),exceptions=sum(r['status']!='success' for r in rows)))
        by={(r['case'],r['problem'],r['num_cores'],r['policy']):r for r in rows if r['status']=='success'}
        for problem in (1,2,3):
            for method in ('joint','region','reads'):
                pairs=[(r,by.get((c,p,n,'legacy'))) for (c,p,n,m),r in by.items() if p==problem and m==method]
                pairs=[(r,b) for r,b in pairs if b]
                if not pairs:continue
                gains=[100*(1-r['after']/b['after']) for r,b in pairs]
                comparisons.append(dict(group=group,problem=problem,method=method,control='legacy_neighborhood_same_loop',
                    pairs=len(pairs),wins=sum(r['after']<b['after'] for r,b in pairs),ties=sum(r['after']==b['after'] for r,b in pairs),
                    losses=sum(r['after']>b['after'] for r,b in pairs),mean_paired_reduction_pct=statistics.mean(gains),
                    median_paired_reduction_pct=statistics.median(gains),
                    mean_gain_over_input_pct=statistics.mean(r['reduction_pct'] for r,b in pairs)))
    write_csv(a.out/'区域搜索逐图比较.csv',all_rows);write_csv(a.out/'区域搜索方法比较.csv',comparisons)
    write_csv(a.out/'区域搜索调用曲线.csv',curves);write_csv(a.out/'区域搜索成本.csv',costs)
    atomic_json(a.out/'区域搜索分析.json',dict(comparisons=comparisons,costs=costs,
        scope='fixed recorded incumbents; extra-budget search, not from-scratch generalization; failures retained'))
    print(json.dumps(dict(comparisons=comparisons,costs=costs),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
