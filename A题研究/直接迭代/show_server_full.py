"""Read-only progress display; runnable on the remote Linux host."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--root',type=Path,required=True)
p.add_argument('--pid-file',type=Path)
a=p.parse_args()
print('服务器全量实验进度',datetime.now().isoformat(timespec='seconds'))
if a.pid_file and a.pid_file.exists():
    pid=int(a.pid_file.read_text().strip())
    try:
        os.kill(pid,0)
        stat=Path(f'/proc/{pid}/stat')
        alive=not(stat.exists() and stat.read_text().split(') ')[1].startswith('Z'))
    except ProcessLookupError:alive=False
    print('调度进程：', '运行中' if alive else '已退出', 'PID',pid)
for phase,label,total in [('replay','现有1500方案逐份官方复评',1500),
                          ('cold','P1全100图×5核数×2方法',1000),
                          ('cold_verification','新旧方法最终方案独立复评',1000)]:
    path=a.root/phase/'progress.json'
    if not path.exists():
        print(f'{label}：尚未启动，计划最多{total}项')
        continue
    s=json.loads(path.read_text())
    print(f"{label}：{s['completed']}/{s['expected']}；成功{s['success']}；需处理{s['attention']}；运行{len(s['active'])}")
    print(f"  已结束任务记录评测{s['recorded_calls']}次；更新{s['updated']}")
    if s['active']:print('  当前：'+', '.join(s['active'][:8]))
if (a.root/'attention.json').exists():
    print('需要处理：',json.loads((a.root/'attention.json').read_text()))
print('冷启动每项是一整次搜索，并非一份候选；全量有效结论需要同时检查失败项。')
