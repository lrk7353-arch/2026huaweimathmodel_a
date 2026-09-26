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
pid_files=[('主批次调度',a.pid_file),('超时补跑调度',a.root.parent/'timeout_retry.pid'),
           ('补跑后恢复调度',a.root.parent/'resume_after_retry.pid')]
for label,pid_file in pid_files:
    if not pid_file or not pid_file.exists():continue
    pid=int(pid_file.read_text().strip())
    try:
        os.kill(pid,0)
        stat=Path(f'/proc/{pid}/stat')
        alive=not(stat.exists() and stat.read_text().split(') ')[1].startswith('Z'))
        stopped=stat.exists() and stat.read_text().split(') ')[1].startswith(('T','t'))
    except ProcessLookupError:alive=False
    print(label+'：', ('已暂停' if stopped else '运行中') if alive else '已退出', 'PID',pid)
if (a.root/'user_pause.json').exists():
    print('用户暂停：已停止派发新搜索；以下旧进度可能滞后，以任务结果文件为准。')
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
retry=a.root/'replay_timeout_retry/progress.json'
if retry.exists():
    s=json.loads(retry.read_text())
    print(f"超时补跑（原失败记录保留）：{s['completed']}/{s['expected']}；通过{s['success']}；需处理{s['attention']}；运行{len(s['active'])}")
proof=a.root/'replay_reconciliation.json'
if proof.exists():
    s=json.loads(proof.read_text())
    print(f"合并独立复评证据后：{s['verified']}/1500已验证；共{s['original_calls']+s['additional_calls']}次官方调用")
if (a.root/'attention.json').exists():
    attention=json.loads((a.root/'attention.json').read_text())
    print('已解决提示：' if attention.get('resolved') else '原批次提示：',attention)
print('冷启动每项是一整次搜索，并非一份候选；全量有效结论需要同时检查失败项。')
