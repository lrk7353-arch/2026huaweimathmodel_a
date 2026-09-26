"""Build the frozen paper tables and plans from verified, immutable evidence only."""
from pathlib import Path
import csv,json,tarfile,io,statistics,hashlib,sys
ROOT=Path(__file__).resolve().parents[2]; OUT=ROOT/'论文主体版本_v1_20260926'; D=ROOT/'A题研究/直接迭代'
def readcsv(p):return list(csv.DictReader(p.open(encoding='utf-8-sig')))
def writecsv(p,rows):
 p.parent.mkdir(parents=True,exist_ok=True)
 with p.open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
def main():
 src=D/'P1多尺度联合优化_20260926/交付1500';out=OUT/'最终方案1500';out.mkdir(exist_ok=True)
 rows=readcsv(src/'累计1500配置成绩.csv');metrics=readcsv(D/'服务器全量_20260926/1500结果分析/1500复评明细.csv')
 key=lambda r:(r['case'],int(r['problem']),int(r['cores']))
 tab={key(r):r for r in rows};mt={key(r):r for r in metrics};assert len(tab)==len(mt)==1500
 with tarfile.open(src/'selected_plans.tar.gz') as a:plans={m.name:a.extractfile(m).read() for m in a if m.isfile()}
 changes=[]
 for name in ['case_044_p1_n4','case_083_p1_n5']:
  f=D/'服务器全量_20260926/跨核保底核查'/f'{name}.json';v=json.loads(f.read_text());rec=v['record'];assert rec['status']=='success'
  k=(v['case'],v['problem'],v['target_cores']);r=tab[k];m=mt[k];oldtime=int(r['makespan']);oldcopy=int(r['added_copy']);rm=rec['metrics'];t=rm['makespan'];copy=rm['data_movement_bytes']['added_copy_bytes'];assert (t,copy)<(oldtime,oldcopy)
  oldpath=Path(rec['plan_path']);rel=oldpath.relative_to('/Users/liyu/Desktop/A题P1最新审阅');payload=(ROOT/rel).read_bytes();assert len(json.loads(payload)['core_schedules'])==k[2]
  plans[r['plan']]=payload
  r.update(makespan=str(t),added_copy=str(copy),speedup=str(int(r['original_singlecore'])/t),source='verified_lower_core_transfer',source_plan=str(rel),verification='official_success_separate_transfer')
  m.update(makespan=str(t),added_copy=str(copy),speedup=r['speedup'],partition_copy=str(rm['data_movement_bytes']['partition_added_copy_bytes']),spill_copy=str(rm['data_movement_bytes']['spill_added_copy_bytes']),active_cores=str(rm['active_cores']),record_path=str(f.relative_to(ROOT)),evaluator_seconds=str(rec['evaluation_elapsed_seconds']),recovered_from_timeout='False')
  caps=rm['capacity_bytes'];peaks=rm['memory_peak_by_core'];m['l1_peak_ratio']=str(max(p['L1'] for p in peaks.values())/caps['L1']);m['ub_peak_ratio']=str(max(p['UB'] for p in peaks.values())/caps['UB']);m['copy_ratio']=str((int(m['original_copy'])+copy)/int(m['original_copy'])) if int(m['original_copy']) else ''
  changes.append(dict(case=k[0],problem=k[1],cores=k[2],old_makespan=oldtime,new_makespan=t,old_added_copy=oldcopy,new_added_copy=copy,evidence=str(f.relative_to(ROOT))))
 assert len(plans)==1500
 for r in rows:
  assert r['plan'] in plans
  r['paper_plan_sha256']=hashlib.sha256(plans[r['plan']]).hexdigest()
 with tarfile.open(out/'selected_plans.tar.gz','w:gz') as a:
  for n,b in sorted(plans.items()):
   ti=tarfile.TarInfo(n);ti.size=len(b);ti.mtime=0;a.addfile(ti,io.BytesIO(b))
 writecsv(out/'累计1500配置成绩.csv',rows);writecsv(out/'本次两项入库.csv',changes)
 # P2 without L2 has no cache hit-rate; explicit N/A, not fabricated zeros.
 for m in metrics:
  if int(m['problem'])!=3:
   for c in ['cache_hit_bytes','cache_miss_bytes','cache_byte_hit_rate']:m[c]='N/A'
 writecsv(OUT/'数据表/附录A_1500配置完整指标.csv',metrics)
 curves=[]
 for p in range(1,4):
  for k in range(1,6):
   sub=[r for r in rows if int(r['problem'])==p and int(r['cores'])==k]
   curves.append(dict(problem=p,cores=k,n=100,statement_speedup=1.0 if k==1 else statistics.mean(float(r['speedup']) for r in sub),raw_singlecore_ratio_mean=statistics.mean(float(r['speedup']) for r in sub),mean_makespan=statistics.mean(int(r['makespan']) for r in sub),mean_added_copy=statistics.mean(int(r['added_copy']) for r in sub),scope='cumulative selected; not equal-budget cold run'))
 writecsv(OUT/'数据表/正文_核数曲线.csv',curves)
 pairs=[];summary=[]
 for k in range(1,6):
  for i in range(1,101):
   a=tab[(f'case_{i:03}',2,k)];b=tab[(f'case_{i:03}',3,k)];m=mt[key(b)];ta=int(a['makespan']);tb=int(b['makespan'])
   pairs.append(dict(case=a['case'],cores=k,p2_makespan=ta,p3_makespan=tb,p2_over_p3=ta/tb,p2_added_copy=a['added_copy'],p3_added_copy=b['added_copy'],p2_cache_hit_rate='N/A',p3_cache_hit_rate=m['cache_byte_hit_rate'],p3_hit_bytes=m['cache_hit_bytes'],p3_miss_bytes=m['cache_miss_bytes'],relation='faster' if ta>tb else 'equal' if ta==tb else 'slower'))
  sub=[r for r in pairs if r['cores']==k];summary.append(dict(cores=k,n=100,mean_same_core_speedup=statistics.mean(r['p2_over_p3'] for r in sub),faster=sum(r['relation']=='faster' for r in sub),equal=sum(r['relation']=='equal' for r in sub),slower=sum(r['relation']=='slower' for r in sub),scope='separately optimized plans; combined scheduling/cache comparison'))
 writecsv(OUT/'数据表/附录B_P3同核500组.csv',pairs);writecsv(OUT/'数据表/正文_P3同核汇总.csv',summary)
 info={'base_verified_on_server':1500,'base_replay_calls':1514,'new_transfer_plans':2,'retained_base_plans':1498,'new_search_calls_for_packaging':0,'five_core':{str(p):statistics.mean(float(r['speedup']) for r in rows if int(r['problem'])==p and int(r['cores'])==5) for p in range(1,4)},'scope':'cumulative best within recorded verified library; not global optimum; no claim of uniform budget'}
 (out/'统计说明.json').write_text(json.dumps(info,ensure_ascii=False,indent=2));print(json.dumps(info,ensure_ascii=False))
if __name__=='__main__':main()
