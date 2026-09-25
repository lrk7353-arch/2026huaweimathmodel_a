"""Receive independently replayed cold improvements without losing warm winners."""
import csv,json,sys
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor,as_completed
HERE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(HERE.parent))
from common_run import DATA,read_json,atomic_json,evaluate,write_csv,score


def replay(item):
    r=item['record'];new=evaluate(DATA/(item['case']+'.json'),read_json(r['plan_path']),item['problem'],
        HERE/'从头精选复评'/'evaluations'/f"{item['case']}_p{item['problem']}_n{item['cores']}",timeout=120,config_path=DATA/'config.txt')
    assert new['status']=='success' and score(new)==score(r),(item,new)
    assert new['hashes']['plan_sha256']==r['hashes']['plan_sha256']
    return dict(item,replay_record=new)


def main():
    ledger=list(csv.DictReader((HERE/'累计1500配置成绩.csv').open(encoding='utf-8-sig')))
    old={(r['case'],int(r['problem']),int(r['cores'])):r for r in ledger};chosen={}
    for row in csv.DictReader((HERE/'从头对照/逐臂结果.csv').open(encoding='utf-8-sig')):
        r=read_json(row['summary'])['best_record'];key=row['case'],int(row['problem']),int(row['cores'])
        if r is None or score(r)>=(int(old[key]['makespan']),int(old[key]['added_copy'])):continue
        if key not in chosen or score(r)<score(chosen[key]['record']):chosen[key]=dict(case=key[0],problem=key[1],cores=key[2],variant=row['variant'],record=r)
    confirmed=[]
    with ProcessPoolExecutor(max_workers=4) as pool:
        for f in as_completed([pool.submit(replay,item) for item in chosen.values()]):
            item=f.result();confirmed.append(item);print(item['case'],item['problem'],item['cores'],item['variant'],score(item['record']),flush=True)
    atomic_json(HERE/'从头精选复评/记录.json',dict(logical_calls=len(confirmed),records=confirmed))
    for item in confirmed:
        key=item['case'],item['problem'],item['cores'];r=old[key];rec=item['replay_record'];plan=read_json(rec['plan_path'])
        dest=Path('精选方案')/f'p{key[1]}'/f'n{key[2]}'/f'{key[0]}_multicore_res.json';atomic_json(HERE/dest,plan)
        r.update(makespan=score(rec)[0],added_copy=score(rec)[1],speedup=int(r['original_singlecore'])/score(rec)[0],
            source='three_metric_cold_'+item['variant'],source_commit='0427ae4',source_plan=item['record']['plan_path'],
            plan=str(dest),plan_sha256=rec['hashes']['plan_sha256'],verification='independent_replay_exact_match')
    write_csv(HERE/'累计1500配置成绩.csv',ledger)
    single={(r['case'],int(r['problem'])):int(r['makespan']) for r in ledger if int(r['cores'])==1}
    curves=[]
    for p in (1,2,3):
        for n in range(1,6):
            rs=[r for r in ledger if int(r['problem'])==p and int(r['cores'])==n]
            curves.append(dict(problem=p,cores=n,graphs=100,mean_speedup=sum(float(r['speedup']) for r in rs)/100,
                optimized_singlecore_normalized_speedup=sum(single[r['case'],p]/int(r['makespan']) for r in rs)/100))
    write_csv(HERE/'累计核数曲线.csv',curves)
    # Refresh P3 diagnostics with current delivered five-core plans, never zero-fill.
    observation=read_json(HERE/'当前P3观测.json')['records']
    for item in read_json(HERE/'精选复评/独立记录.json')['records']:
        if item['problem']==3:observation[item['case']]=dict(record=item['replay_record'])
    for item in confirmed:
        if item['problem']==3 and item['cores']==5:observation[item['case']]=dict(record=item['replay_record'])
    cache_rows=[]
    for case,v in sorted(observation.items()):
        rec=v['record'];m=rec['metrics'];c=m['cache_stats'];d=m['data_movement_bytes'];row=old[case,3,5]
        assert (m['makespan'],d['added_copy_bytes'])==(int(row['makespan']),int(row['added_copy']))
        cache_rows.append(dict(case=case,makespan=m['makespan'],copy=d['added_copy_bytes'],spill=d['spill_added_copy_bytes'],
            hit_bytes=c['hit_bytes'],miss_bytes=c['miss_bytes'],hit_rate=c['hit_rate'],plan_sha256=row['plan_sha256']))
    write_csv(HERE/'交付100图P3五核三指标.csv',cache_rows)
    atomic_json(HERE/'最终交付摘要.json',dict(cold_confirmed=len(confirmed),curves=curves,
        p3_mean_hit_rate=sum(r['hit_rate'] for r in cache_rows)/100,
        p3_pooled_hit_rate=sum(r['hit_bytes'] for r in cache_rows)/sum(r['hit_bytes']+r['miss_bytes'] for r in cache_rows),
        scope='Cumulative selected library. Two curve denominators explicitly distinguished.'))


if __name__=='__main__':main()
