"""Recover exact-plan P3 metrics from history; replay only missing observations."""
import csv
import gzip
import hashlib
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from common_run import DATA, R, atomic_json, evaluate, known, read_json, write_csv

BASE = Path(__file__).parent
OUT = BASE / '三指标联合推进_20260926'
FINAL = BASE / '联合整合_20260926/最终累计'


def replay(row):
    plan = read_json(FINAL / row['plan'])
    rec = evaluate(DATA / (row['case']+'.json'), plan, 3,
                   OUT/'观测复评'/row['case'], timeout=120, config_path=DATA/'config.txt')
    assert rec['status'] == 'success', (row['case'], rec)
    assert (rec['metrics']['makespan'], rec['metrics']['data_movement_bytes']['added_copy_bytes']) == (
        int(row['makespan']), int(row['added_copy'])), row['case']
    return row['case'], rec


def main():
    OUT.mkdir(exist_ok=True)
    index = {}
    def add(rec):
        if rec and rec.get('status') == 'success' and rec.get('problem') == 3 and rec['metrics']['num_cores'] == 5:
            index[rec['hashes']['graph_sha256'], rec['hashes']['plan_sha256']] = rec
    for rec in known().values(): add(rec)
    for item in read_json(BASE/'联合整合_20260926/累计并集/复评记录.json')['records']: add(item['record'])
    for item in read_json(BASE/'联合整合_20260926/新增精选复评/结果.json')['records']: add(item['replay_record'])
    with gzip.open(BASE/'联合整合_20260926/同预算对照/全部调用记录.json.gz','rt') as f:
        for item in json.load(f): add(item['record'])
    rows = [r for r in csv.DictReader((FINAL/'累计1500配置成绩.csv').open(encoding='utf-8-sig'))
            if r['problem']=='3' and r['cores']=='5']
    records = {}; missing=[]
    for row in rows:
        graph_hash=hashlib.sha256((DATA/(row['case']+'.json')).read_bytes()).hexdigest()
        rec=index.get((graph_hash,row['plan_sha256']))
        if rec: records[row['case']]=dict(source='exact_historical_hash',record=rec)
        else: missing.append(row)
    recovered=len(records)
    print(json.dumps(dict(historical=recovered,replays_needed=len(missing))),flush=True)
    with ProcessPoolExecutor(max_workers=4) as pool:
        for future in as_completed([pool.submit(replay,row) for row in missing]):
            case,rec=future.result();records[case]=dict(source='fixed_plan_replay',record=rec)
            atomic_json(OUT/'当前P3观测.json',dict(complete=False,records=records))
            print(json.dumps(dict(case=case,done=len(records),makespan=rec['metrics']['makespan'])),flush=True)
    output=[]
    for row in rows:
        m=records[row['case']]['record']['metrics'];d=m['data_movement_bytes'];c=m['cache_stats']
        assert (m['makespan'],d['added_copy_bytes'])==(int(row['makespan']),int(row['added_copy']))
        output.append(dict(case=row['case'],cores=5,makespan=m['makespan'],added_copy_bytes=d['added_copy_bytes'],
            spill_bytes=d['spill_added_copy_bytes'],hit_bytes=c['hit_bytes'],miss_bytes=c['miss_bytes'],
            hit_rate=c['hit_rate'],source=records[row['case']]['source'],plan_sha256=row['plan_sha256']))
    atomic_json(OUT/'当前P3观测.json',dict(complete=True,historical=recovered,
        replay_calls=len(missing),new_calls=sum(not v['record']['cache_hit'] for v in records.values()
            if v['source']=='fixed_plan_replay'),records=records))
    write_csv(OUT/'当前100图P3五核三指标.csv',output)
    print('Complete: 100 exact-plan P3 observations',flush=True)


if __name__=='__main__': main()
