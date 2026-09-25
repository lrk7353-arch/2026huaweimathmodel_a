from pathlib import Path
from datetime import datetime
import json,csv
R=Path(__file__).resolve().parent/'运行结果'
print('直接迭代进度 '+datetime.now().strftime('%m-%d %H:%M:%S'))
export=R.parent/'第十三轮成果/summary.json'
if not export.exists():export=R.parent/'第十二轮成果/summary.json'
if not export.exists():export=R.parent/'第十一轮成果/summary.json'
if not export.exists():export=R.parent/'第十轮成果/summary.json'
if not export.exists():export=R.parent/'第九轮成果/summary.json'
if not export.exists():export=R.parent/'第八轮成果/summary.json'
if not export.exists():export=R.parent/'第七轮成果/summary.json'
if not export.exists():export=R.parent/'第六轮成果/summary.json'
if not export.exists():export=R.parent/'第五轮成果/summary.json'
if not export.exists():export=R.parent/'第四轮成果/summary.json'
if not export.exists():export=R.parent/'第三轮成果/summary.json'
if not export.exists():export=R.parent/'第二轮成果/summary.json'
if not export.exists():export=R.parent/'本轮成果/summary.json'
if export.exists():
 s=json.loads(export.read_text())
 print(f"已导出 {export.parent.name}：覆盖 {s['coverage']}/1500，该轮耗时改善 {s['strict_time_improved']} 个配置。")
 catalog=export.parent/'全部成绩.csv'
 with catalog.open(encoding='utf-8-sig') as f:coverage={(r['case'],int(r['problem']),int(r['cores'])) for r in csv.DictReader(f)}
 for p in list(R.glob('推广*/slots/case_*/p*_n*/summary.json'))+list(R.glob('深化*/slots/case_*/p*_n*/summary.json'))+list(R.glob('融合*/slots/case_*/p*_n*/summary.json'))+list(R.glob('综合*/slots/case_*/p*_n*/summary.json')):
  r=json.loads(p.read_text()).get('best_record')
  if r and r.get('status')=='success':coverage.add((Path(r['graph_path']).stem,r['problem'],r['metrics']['num_cores']))
 print(f'综合导出与各批次记录，已有官方可行方案 {len(coverage)}/1500。')
for d in sorted(R.iterdir()) if R.exists() else []:
 if not d.is_dir():continue
 if '计划' in d.name or '最终比较' in d.name:continue
 p=d/'summary.json';final=p.exists()
 if not final:p=d/'progress.json'
 if not p.exists():
  child=list(d.glob('case_*/summary.json'))+list(d.glob('slots/case_*/p*_n*/summary.json'))+list(d.glob('slots/case_*/p*_n*/*/summary.json'))
  if child:print(d.name+f'：已有 {len(child)} 个单例结果')
  else:print(d.name+'：仅有目录，未发现批次进度记录')
  continue
 s=json.loads(p.read_text());rows=s.get('rows',[]);stamp=datetime.fromtimestamp(p.stat().st_mtime).strftime('%H:%M:%S')
 if 'complete_cases' in s:detail=f"四格齐全 {s['complete_cases']}/100"
 elif final and rows:detail=f"已返回 {len(rows)} 项"
 elif 'completed' in s:detail=f"{s['completed']}/{s.get('expected','?')}"
 elif 'improved' in s:detail=f"改善 {s['improved']}/{s.get('proposals','?')}"
 else:detail='结果已保存'
 state='已调整排期，结果保留' if s.get('interrupted') else '批次结束' if final and s.get('complete') else '已有部分结果' if final else '尚未汇总'
 if s.get('superseded_by'):state='已修复并重跑，正式结果见 '+s['superseded_by']
 if final and s.get('best_record') and 'elapsed_seconds' in s:state='单图搜索结束'
 if 'expected' in s:detail=f"已返回 {s.get('completed',len(rows))}/{s['expected']} 项"
 failed=s.get('failed',len(s.get('failures',[])))
 if failed:detail+=f'，本批未成功 {failed} 项（可能由后续批次补齐）'
 print(f'{d.name}：{state}，{detail}，更新 {stamp}')
 if d.name.startswith('区域搜索') and not final:
  for progress in sorted(d.glob('slots/case_*/p*_n*/*/progress.json')):
   slot=json.loads(progress.read_text())
   if not slot.get('complete'):
    print(f"  {slot['case']} P{slot['problem']} {slot['policy']}：{slot['logical_calls']}/{slot['budget']}次，{slot['before']}→{slot['after']}周期")
 if d.name.startswith('端到端') and not final:
  batch_plan=json.loads((d/'plan.json').read_text()) if (d/'plan.json').exists() else {}
  for progress in sorted(d.glob('slots/case_*/p*_n*/*/progress.json')):
   if (progress.parent/'summary.json').exists():continue
   slot=json.loads(progress.read_text())
   if slot.get('complete'):continue
   current=slot
   if slot.get('phase')=='prefix' and (progress.parent/'prefix/progress.json').exists():
    current=json.loads((progress.parent/'prefix/progress.json').read_text())
   calls=current.get('logical_calls',current.get('calls',0));limit=slot.get('budget',batch_plan.get('budget','?'))
   best=current.get('best_record') or current.get('best')
   if best and 'record' in best:best=best['record']
   value=best.get('metrics',{}).get('makespan') if best else None
   phase='初始搜索' if slot.get('phase')=='prefix' else '求解中'
   print(f"  {progress.parent.parent.parent.name} {progress.parent.parent.name} {progress.parent.name}：{phase}，{calls}/{limit}次，当前周期 {value if value is not None else '尚无可行记录'}")
print('这是磁盘进度；每次打开重新读取。候选结束后更新，不代表每秒刷新。')
print('目录存在不表示后台正在运行；进程状态需另行检查。')
print('批次结束表示已返回记录；单图可能触及时间上限，详情见对应成果目录的结果报告。')
