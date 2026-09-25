"""Independently replay all selected improvements and publish a small overlay."""
import csv
import hashlib
import json
from pathlib import Path
import sys
from concurrent.futures import ProcessPoolExecutor,as_completed
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parent))
from common_run import DATA,read_json,atomic_json,evaluate,write_csv,score


def worker(item):
    r=item['record'];plan=read_json(r['plan_path'])
    replay=evaluate(DATA/(item['case']+'.json'),plan,item['problem'],
        HERE/'精选复评'/'evaluations'/f"{item['case']}_p{item['problem']}",timeout=120,config_path=DATA/'config.txt')
    assert replay['status']=='success' and score(replay)==score(r),(item,replay)
    assert replay['hashes']['plan_sha256']==r['hashes']['plan_sha256']
    return dict(item,replay_record=replay)


def main():
    items=read_json(HERE/'集中对照/改善记录.json')['records'];chosen={}
    for item in items:
        key=item['case'],item['problem']
        if key not in chosen or score(item['record'])<score(chosen[key]['record']):chosen[key]=item
    confirmed=[]
    with ProcessPoolExecutor(max_workers=4) as pool:
        for future in as_completed([pool.submit(worker,item) for item in chosen.values()]):
            item=future.result();confirmed.append(item);print(item['case'],item['problem'],item['policy'],item['before'],item['after'],flush=True)
    atomic_json(HERE/'精选复评/独立记录.json',dict(logical_calls=len(confirmed),passed=len(confirmed),records=confirmed))
    lookup={(x['case'],x['problem']):x for x in confirmed}
    previous=HERE.parent/'联合整合_20260926/最终累计'
    ledger=list(csv.DictReader((previous/'累计1500配置成绩.csv').open(encoding='utf-8-sig')))
    for r in ledger:
        r['plan']='../联合整合_20260926/最终累计/'+r['plan']
        key=r['case'],int(r['problem'])
        if r['cores']!='5' or key not in lookup:continue
        item=lookup[key];rec=item['replay_record'];plan=read_json(rec['plan_path'])
        dest=Path('精选方案')/f'p{key[1]}'/'n5'/f'{key[0]}_multicore_res.json'
        atomic_json(HERE/dest,plan)
        r.update(makespan=score(rec)[0],added_copy=score(rec)[1],speedup=int(r['original_singlecore'])/score(rec)[0],
            source='three_metric_'+item['policy'],source_commit='d5f3f67',source_plan=item['record']['plan_path'],
            plan=str(dest),plan_sha256=rec['hashes']['plan_sha256'],verification='independent_replay_exact_match')
    write_csv(HERE/'累计1500配置成绩.csv',ledger)
    curves=[]
    for p in (1,2,3):
        for n in range(1,6):
            rs=[r for r in ledger if int(r['problem'])==p and int(r['cores'])==n]
            assert len(rs)==100
            curves.append(dict(problem=p,cores=n,graphs=100,mean_speedup=sum(float(r['speedup']) for r in rs)/100))
    write_csv(HERE/'累计核数曲线.csv',curves)
    atomic_json(HERE/'精选复评/交付摘要.json',dict(confirmed=len(confirmed),curves=curves,
        scope='Cumulative selected-plan overlay, not cold-start algorithm score; plan paths relative to this directory.'))


if __name__=='__main__':main()
