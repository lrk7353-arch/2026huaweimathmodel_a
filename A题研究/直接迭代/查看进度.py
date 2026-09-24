from pathlib import Path
from datetime import datetime
import json
R=Path(__file__).resolve().parent/'运行结果'
print('直接迭代进度 '+datetime.now().strftime('%m-%d %H:%M:%S'))
export=R.parent/'本轮成果/summary.json'
if export.exists():
 s=json.loads(export.read_text())
 print(f"已导出本轮成果：覆盖 {s['coverage']}/1500，耗时改善 {s['strict_time_improved']} 个配置。")
for d in sorted(R.iterdir()) if R.exists() else []:
 if not d.is_dir():continue
 p=d/'summary.json';final=p.exists()
 if not final:p=d/'progress.json'
 if not p.exists():
  child=list(d.glob('case_*/summary.json'))
  if child:print(d.name+f'：已有 {len(child)} 个单例结果')
  else:print(d.name+'：准备中')
  continue
 s=json.loads(p.read_text());rows=s.get('rows',[]);stamp=datetime.fromtimestamp(p.stat().st_mtime).strftime('%H:%M:%S')
 if 'complete_cases' in s:detail=f"四格齐全 {s['complete_cases']}/100"
 elif final and rows:detail=f"已返回 {len(rows)} 项"
 elif 'completed' in s:detail=f"{s['completed']}/{s.get('expected','?')}"
 elif 'improved' in s:detail=f"改善 {s['improved']}/{s.get('proposals','?')}"
 else:detail='结果已保存'
 state='批次结束' if final and s.get('complete') else '已有部分结果' if final else '运行中'
 print(f'{d.name}：{state}，{detail}，更新 {stamp}')
print('这是磁盘进度；每次打开重新读取。候选结束后更新，不代表每秒刷新。')
print('批次结束表示已返回记录；单图可能触及时间上限，详情见本轮结果.md。')
