import csv,gzip,json,sys
from pathlib import Path
HERE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(HERE.parent))
from common_run import read_json
with gzip.open(HERE/'集中对照/全部调用.json.gz','rt') as f:calls=json.load(f)
for case,p in [('case_047',1),('case_009',2),('case_053',2),('case_023',3),('case_046',3)]:
    print(case,p)
    for c in calls:
        if c['case']==case and c['problem']==p and c['accepted']:
            m=c['record']['metrics'];d=m['data_movement_bytes'];meta=c['metadata']
            print(c['policy'],c['call'],c['name'],m['makespan'],d['added_copy_bytes'],d['spill_added_copy_bytes'],
                  m.get('cache_stats',{}).get('hit_rate'),json.dumps(meta,ensure_ascii=False)[:850])
rows=list(csv.DictReader((HERE/'当前100图P3五核三指标.csv').open(encoding='utf-8-sig')))
print('P3 current100 mean_hit',sum(float(r['hit_rate']) for r in rows)/100,
      'pooled_hit',sum(int(r['hit_bytes']) for r in rows)/sum(int(r['hit_bytes'])+int(r['miss_bytes']) for r in rows))
print('warm calls',len(calls),'generation_errors',sum(c['record']['status']!='success' for c in calls))
