import csv,gzip,json,math,statistics,sys
from pathlib import Path
HERE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(HERE.parent))
from common_run import read_json,atomic_json,write_csv


def main():
    out=HERE/'从头对照';execution=read_json(out/'execution.json');assert execution['complete']
    rows=list(csv.DictReader((out/'逐臂结果.csv').open(encoding='utf-8-sig')))
    for r in rows:
        for k in ('problem','cores','makespan','added_copy','logical_calls','failures','generation_failures'):r[k]=int(r[k]) if r[k] else None
        r['elapsed_seconds']=float(r['elapsed_seconds'])
    pairs=[];gates=[];allcalls=[];stages=[]
    for p in sorted({r['problem'] for r in rows}):
        grouped={}
        for group in ('development','decision','lowcore'):
            selected=[r for r in rows if r['problem']==p and r['group']==group]
            lookup={(r['case'],r['cores'],r['variant']):r for r in selected}
            scores=[];wins=ties=losses=bigloss=0
            for r in [r for r in selected if r['variant']=='wait_integrated']:
                b=lookup[r['case'],r['cores'],'strong']
                ratio=b['makespan']/r['makespan'] if b['makespan'] and r['makespan'] else 0
                scores.append(ratio);wins+=ratio>1;ties+=ratio==1;losses+=ratio<1;bigloss+=ratio<1/1.02
                pairs.append(dict(problem=p,group=group,case=r['case'],cores=r['cores'],strong=b['makespan'],
                    wait_integrated=r['makespan'],time_reduction_pct=100*(1-r['makespan']/b['makespan']),
                    strong_calls=b['logical_calls'],new_calls=r['logical_calls'],strong_seconds=b['elapsed_seconds'],new_seconds=r['elapsed_seconds']))
            grouped[group]=dict(geomean_speed_gain=math.exp(statistics.mean(math.log(x) for x in scores))-1 if all(scores) else -1,
                wins=wins,ties=ties,losses=losses,regress_over2pct=bigloss)
        new=[r for r in rows if r['problem']==p and r['variant']=='wait_integrated']
        old=[r for r in rows if r['problem']==p and r['variant']=='strong']
        rate=sum(r['failures'] for r in new)/max(1,sum(r['logical_calls'] for r in new))
        med=statistics.median(r['elapsed_seconds'] for r in new);oldmed=statistics.median(r['elapsed_seconds'] for r in old)
        d=grouped['decision'];low=grouped['lowcore']
        passed=d['geomean_speed_gain']>=.005 and d['wins']>=3 and d['regress_over2pct']<=1 and low['geomean_speed_gain']>=-.005 and rate<=.05 and med<=2*oldmed+5
        gates.append(dict(problem=p,groups=grouped,failure_rate=rate,generation_failures=sum(r['generation_failures'] for r in new),
            median_seconds=med,strong_median_seconds=oldmed,cost_scope='all16 configs in scene',passed=passed))
    for r in rows:
        s=read_json(r['summary'])
        allcalls.extend(dict(case=r['case'],problem=r['problem'],cores=r['cores'],group=r['group'],variant=r['variant'],call=i+1,**c)
            for i,c in enumerate(s['calls']))
        stages.append(dict(case=r['case'],problem=r['problem'],cores=r['cores'],variant=r['variant'],
            generations=s.get('generations'),stages=s.get('stages'),routing=s.get('routing')))
    for name,value in [('全部调用',allcalls),('生成及路由',stages)]:
        with gzip.open(out/(name+'.json.gz'),'wt',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,separators=(',',':'))
    write_csv(out/'逐配置比较.csv',pairs)
    atomic_json(out/'晋级判定.json',dict(scenes=gates,calls=len(allcalls),failures=sum(c['record']['status']!='success' for c in allcalls),execution=execution))
    print(json.dumps(gates,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
