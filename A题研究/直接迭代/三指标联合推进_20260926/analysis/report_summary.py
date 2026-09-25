"""Recompute compact report facts from persisted experiment evidence."""
import csv
import gzip
import json
from collections import Counter
from pathlib import Path

HERE=Path(__file__).resolve().parents[1]
read=lambda p:json.loads((HERE/p).read_text(encoding='utf-8'))


def main():
    costs={'missing_P3_replays':64,'warm_calls':read('集中对照/晋级判定.json')['logical_calls'],
           'four_cell_controls':read('归因/官方记录.json')['logical_calls'],
           'warm_selection_replays':read('精选复评/独立记录.json')['logical_calls'],
           'cold_calls':read('从头对照/晋级判定.json')['calls'],
           'whole_gain_controls':read('主要收益归因/记录.json')['logical_calls'],
           'cold_selection_replays':read('从头精选复评/记录.json')['logical_calls']}
    result={'costs':costs,'total_official_calls':sum(costs.values()),'synthetic_test_calls_excluded':True}
    with gzip.open(HERE/'从头对照/全部调用.json.gz','rt',encoding='utf-8') as f:calls=json.load(f)
    result['cold_failure_status']=dict(Counter(c['record']['status'] for c in calls))
    result['cold_arm_calls']={v:sum(c['variant']==v for c in calls) for v in ('strong','wait_integrated')}
    result['cold_044_P3_accepts']=[{k:c.get(k) for k in ('variant','call','name','phase','accepted')}|{'time':c['record']['metrics']['makespan']}
        for c in calls if c['case']=='case_044' and c['problem']==3 and c.get('accepted')]
    current=list(csv.DictReader((HERE/'累计1500配置成绩.csv').open(encoding='utf-8-sig')))
    previous=list(csv.DictReader((HERE.parent/'联合整合_20260926/最终累计/累计1500配置成绩.csv').open(encoding='utf-8-sig')))
    result['fivecore']={}
    for p in (1,2,3):
        a=[r for r in previous if int(r['problem'])==p and r['cores']=='5']
        b=[r for r in current if int(r['problem'])==p and r['cores']=='5']
        ma=sum(float(r['speedup']) for r in a)/100;mb=sum(float(r['speedup']) for r in b)/100
        result['fivecore'][p]=dict(before=ma,after=mb,delta=mb-ma,relative_pct=100*(mb/ma-1),
            extra_copy_before=sum(int(r['added_copy']) for r in a),extra_copy_after=sum(int(r['added_copy']) for r in b))
    (HERE/'报告数字.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
