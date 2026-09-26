"""Update delivered P3 metrics and split the paid-plan archive for GitHub limits."""
import csv,hashlib,json,sys
from pathlib import Path
HERE=Path(__file__).resolve().parents[1];ROOT=HERE.parent;sys.path.insert(0,str(ROOT))
from common_run import read_json,atomic_json,write_csv


def main():
    old=ROOT/'真机启发攻坚_20260926'
    cache=list(csv.DictReader((old/'第四批收尾成果/P3五核三指标.csv').open(encoding='utf-8-sig')))
    updates={tuple(r['key']):r['verification_record']for r in read_json(HERE/'独立复评.json')['records']}
    ledger=list(csv.DictReader((HERE/'最终精选1500/累计1500配置成绩.csv').open(encoding='utf-8-sig')))
    rows={(r['case'],int(r['problem']),int(r['cores'])):r for r in ledger}
    assert len(rows)==1500 and set(rows)=={(f'case_{i:03d}',p,n)for i in range(1,101)for p in (1,2,3)for n in range(1,6)}
    for r in cache:
        rec=updates.get((r['case'],3,5))
        if rec:
            m=rec['metrics'];c=m['cache_stats'];d=m['data_movement_bytes']
            r.update(makespan=m['makespan'],copy=d['added_copy_bytes'],spill=d['spill_added_copy_bytes'],
                hit_bytes=c['hit_bytes'],miss_bytes=c['miss_bytes'],hit_rate=c['hit_rate'],plan_sha256=rec['hashes']['plan_sha256'])
        delivered=rows[r['case'],3,5]
        assert r['plan_sha256']==delivered['plan_sha256']
        assert int(r['makespan'])==int(delivered['makespan']) and int(r['copy'])==int(delivered['added_copy'])
    assert len(cache)==100
    write_csv(HERE/'最终精选1500/P3五核三指标.csv',cache)
    baseline=list(csv.DictReader((old/'精选完整1500/累计1500配置成绩.csv').open(encoding='utf-8-sig')))
    metrics=[]
    for p in (1,2,3):
        a=[r for r in baseline if int(r['problem'])==p and r['cores']=='5'];b=[r for r in ledger if int(r['problem'])==p and r['cores']=='5']
        metrics.append(dict(problem=p,old_mean_speedup=sum(float(r['speedup'])for r in a)/100,new_mean_speedup=sum(float(r['speedup'])for r in b)/100,
            old_copy=sum(int(r['added_copy'])for r in a),new_copy=sum(int(r['added_copy'])for r in b)))
    atomic_json(HERE/'最终精选1500/三指标摘要.json',dict(five_core=metrics,p3_mean_hit_rate=sum(float(r['hit_rate'])for r in cache)/100,
        p3_pooled_hit_rate=sum(int(r['hit_bytes'])for r in cache)/sum(int(r['hit_bytes'])+int(r['miss_bytes'])for r in cache)))
    archive=HERE/'正式v2完整审计/all_evaluated_plans.tar.gz';parts=[];digest=hashlib.sha256()
    with archive.open('rb')as stream:
        i=0
        while data:=stream.read(45_000_000):
            path=archive.with_name(archive.name+f'.part{i:03d}');path.write_bytes(data);digest.update(data)
            parts.append(dict(name=path.name,bytes=len(data),sha256=hashlib.sha256(data).hexdigest()));i+=1
    atomic_json(archive.with_name('all_evaluated_plans.parts.json'),dict(archive=archive.name,sha256=digest.hexdigest(),parts=parts,
        restore='Concatenate parts in listed order as raw bytes, verify SHA256, then read as tar.gz. Original unsplit local file is not tracked.'))
    print(json.dumps(dict(metrics=metrics,archive_parts=len(parts)),ensure_ascii=False))


if __name__=='__main__':main()
