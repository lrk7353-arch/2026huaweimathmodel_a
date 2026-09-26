"""Replay selected panel gains and build a complete, portable cumulative package."""
import csv
import hashlib
import io
import json
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
import sys
import tarfile

HERE=Path(__file__).resolve().parents[1];ROOT=HERE.parent
sys.path.insert(0,str(ROOT))
from common_run import DATA,GraphIR,read_json,atomic_json,evaluate,score,validate_plan,write_csv
from solver.common import object_digest
SOURCE=ROOT/'运行结果/持续联合冲刺_20260926/正式75配置_v2_6并发'
OLD=ROOT/'真机启发攻坚_20260926/精选完整1500'
OUT=HERE/'最终精选1500'


def verify(item):
    case,p,n=item['key'];source=item['record']
    out=HERE/'验收复评'/f'{case}_p{p}_n{n}'
    # Resume only our fixed-plan acceptance replay; never feed it into the experiment.
    rec=evaluate(DATA/(case+'.json'),read_json(source['plan_path']),p,out,timeout=180,config_path=DATA/'config.txt')
    assert rec['status']=='success',(item['key'],rec['status'],rec.get('error'))
    assert score(rec)==score(source)
    for k in ('plan_sha256','graph_sha256','config_sha256','official_py_sha256'):
        assert rec['hashes'][k]==source['hashes'][k],(item['key'],k)
    for k in ('data_movement_bytes','cache_stats'):
        assert rec['metrics'].get(k)==source['metrics'].get(k),(item['key'],k)
    return dict(item,verification_record=rec)


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    ledger=list(csv.DictReader((OLD/'累计1500配置成绩.csv').open(encoding='utf-8-sig')))
    before={(r['case'],int(r['problem']),int(r['cores'])):r.copy()for r in ledger}
    imported=read_json(SOURCE/'imported_controls.json')['imported_controls']
    origins={(x['config_id'],x['variant']):x['original_revision']for x in imported}
    chosen={}
    for f in sorted(SOURCE.glob('configurations/*/*/attempt_*/summary.json')):
        s=read_json(f);key=s['case'],s['problem'],s['cores'];record=s.get('best_record')
        if not record or score(record)>=(int(before[key]['makespan']),int(before[key]['added_copy'])):continue
        if key not in chosen or score(record)<score(chosen[key]['record']):
            chosen[key]=dict(key=key,variant=f.parts[-3],source_summary=str(f),
                source_commit=origins.get((f.parts[-4],f.parts[-3]),'6da5b5697497d17b367b760475db97e14328c7ad'),record=record)
    atomic_json(HERE/'接收清单.json',dict(selected=list(chosen.values()),input_ledger_sha256=hashlib.sha256((OLD/'累计1500配置成绩.csv').read_bytes()).hexdigest()))
    confirmed={}
    with ProcessPoolExecutor(max_workers=4)as pool:
        for fut in as_completed([pool.submit(verify,x)for x in chosen.values()]):
            item=fut.result();confirmed[tuple(item['key'])]=item
            print(item['key'],item['variant'],score(item['verification_record']),flush=True)
            atomic_json(HERE/'复评进度.json',dict(done=len(confirmed),expected=len(chosen)))
    atomic_json(HERE/'独立复评.json',dict(logical_calls=len(confirmed),records=list(confirmed.values())))
    gains=[]
    for r in ledger:
        key=r['case'],int(r['problem']),int(r['cores'])
        if key not in confirmed:continue
        item=confirmed[key];rec=item['verification_record'];old=before[key]
        r.update(makespan=score(rec)[0],added_copy=score(rec)[1],speedup=int(r['original_singlecore'])/score(rec)[0],
            source='continuous_panel_'+item['variant'],source_commit=item['source_commit'],source_plan=item['record']['plan_path'],
            plan_sha256=rec['hashes']['plan_sha256'],verification='independent_time_copy_cache_and_input_hash_match')
        gains.append(dict(case=key[0],problem=key[1],cores=key[2],variant=item['variant'],old_time=int(old['makespan']),new_time=score(rec)[0],
            old_copy=int(old['added_copy']),new_copy=score(rec)[1],time_reduction=1-score(rec)[0]/int(old['makespan'])))
    # Stream old archive without extracting paths or materializing the full graph pool.
    byname={r['plan']:r for r in ledger};graphs={};seen=set()
    with tarfile.open(OLD/'selected_plans.tar.gz','r|gz')as oldtar,tarfile.open(OUT/'selected_plans.tar.gz','w:gz')as newtar:
        for member in oldtar:
            assert member.isfile() and member.name in byname and member.name not in seen
            r=byname[member.name];key=r['case'],int(r['problem']),int(r['cores'])
            if key in confirmed:
                value=read_json(confirmed[key]['verification_record']['plan_path'])
                data=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
            else:
                data=oldtar.extractfile(member).read();value=json.loads(data)
            assert object_digest(value)==r['plan_sha256'],key
            if key[0]not in graphs:graphs[key[0]]=GraphIR.from_path(DATA/(key[0]+'.json'))
            validate_plan(graphs[key[0]],value);assert len(value['core_schedules'])==key[2]
            assert (int(r['makespan']),int(r['added_copy']))<=(int(before[key]['makespan']),int(before[key]['added_copy']))
            assert abs(float(r['speedup'])-int(r['original_singlecore'])/int(r['makespan']))<1e-10
            info=tarfile.TarInfo(member.name);info.size=len(data);info.mtime=0
            newtar.addfile(info,io.BytesIO(data));seen.add(member.name)
    assert len(seen)==len(ledger)==1500
    write_csv(OUT/'累计1500配置成绩.csv',ledger);write_csv(HERE/'已验证改善.csv',gains)
    curves=[];lookup={(r['case'],int(r['problem']),int(r['cores'])):int(r['makespan'])for r in ledger}
    for p in (1,2,3):
        for n in range(1,6):
            rs=[r for r in ledger if int(r['problem'])==p and int(r['cores'])==n]
            curves.append(dict(problem=p,cores=n,count=len(rs),mean_speedup=1.0 if n==1 else sum(float(r['speedup'])for r in rs)/100,
                raw_optimized_singlecore_ratio=sum(float(r['speedup'])for r in rs)/100))
    write_csv(OUT/'题目口径核数曲线.csv',curves)
    write_csv(OUT/'P3同核数汇总.csv',[dict(cores=n,mean_P2_over_P3=sum(lookup[f'case_{i:03d}',2,n]/lookup[f'case_{i:03d}',3,n]for i in range(1,101))/100,
        scope='separately optimized selected plans; scheduling and cache combined')for n in range(1,6)])
    atomic_json(OUT/'manifest.json',dict(plans=1500,improved=len(confirmed),strict_time_improvements=sum(x['new_time']<x['old_time']for x in gains),
        tie_time_copy_improvements=sum(x['new_time']==x['old_time']for x in gains),regressions=0,all_plan_hashes_and_legality_verified=True,
        archive_sha256=hashlib.sha256((OUT/'selected_plans.tar.gz').read_bytes()).hexdigest(),
        archive_bytes=(OUT/'selected_plans.tar.gz').stat().st_size,curves=curves,source_revision='6da5b56; imported controls retain original 7175300 provenance',
        scope='Cumulative selected library; not a new complete cold matrix or controller-only score'))
    print(json.dumps(dict(confirmed=len(confirmed),five_core=[x for x in curves if x['cores']==5]),ensure_ascii=False))


if __name__=='__main__':main()
