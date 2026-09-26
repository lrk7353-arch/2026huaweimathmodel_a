"""Continue the already authorized full run only after fixed-plan retries pass."""
from pathlib import Path
import argparse
import subprocess
import sys
import time
from common_run import atomic_json,read_json

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--root',type=Path,required=True)
a=p.parse_args();root=a.root.resolve()
manifest=read_json(root/'run_manifest.json');deadline=manifest['deadline_epoch']
atomic_json(root/'continuation_status.json',dict(status='waiting_for_timeout_retries'))
proof=root/'replay_reconciliation.json'
while not proof.exists() and time.time()+360<deadline:
    time.sleep(5)
if not proof.exists() or not read_json(proof).get('all_verified'):
    atomic_json(root/'continuation_status.json',dict(status='attention',reason='Retries not all verified'))
    raise SystemExit(1)
protocol=manifest['protocol']
cmd=[sys.executable,str(Path(__file__).with_name('run_server_full.py')),
     '--out',str(root),'--delivery',protocol['delivery'],'--stage','all',
     '--workers',str(protocol['workers'])]
with (root.parent/'full_continuation.log').open('a') as out:
    proc=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=out,stderr=out,start_new_session=True)
(root.parent/'full_run.pid').write_text(str(proc.pid)+'\n')
atomic_json(root/'continuation_status.json',dict(status='started',pid=proc.pid,
    original_deadline_preserved=True,original_failed_attempts_preserved=True))
