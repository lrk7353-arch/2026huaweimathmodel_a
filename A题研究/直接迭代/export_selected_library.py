"""Package a complete verified ledger without dependencies on historical paths."""
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile

from common_run import atomic_json, read_json
from solver.common import object_digest


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--ledger', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True); a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    rows = list(csv.DictReader(a.ledger.open(encoding='utf-8-sig')))
    expected = {(f'case_{i:03d}', p, n) for i in range(1, 101) for p in (1, 2, 3) for n in range(1, 6)}
    assert len(rows) == 1500 and {(r['case'], int(r['problem']), int(r['cores'])) for r in rows} == expected
    with tarfile.open(a.out/'selected_plans.tar.gz', 'w:gz') as tar:
        for row in rows:
            original = (a.ledger.parent/row['plan']).resolve()
            plan = read_json(original)
            if object_digest(plan) != row['plan_sha256']: raise ValueError(('plan hash mismatch', row['case']))
            member = f'plans/p{row["problem"]}/n{row["cores"]}/{row["case"]}_multicore_res.json'
            payload = (json.dumps(plan, ensure_ascii=False, separators=(',', ':'))+'\n').encode()
            item = tarfile.TarInfo(member); item.size = len(payload); tar.addfile(item, io.BytesIO(payload))
            row['plan'] = member
    with (a.out/'累计1500配置成绩.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0]); writer.writeheader(); writer.writerows(rows)
    atomic_json(a.out/'manifest.json', dict(complete=True, configurations=len(rows),
        original_ledger=os.path.relpath(a.ledger.resolve(), a.out.resolve()),
        original_ledger_sha256=hashlib.sha256(a.ledger.read_bytes()).hexdigest(),
        archive_sha256=hashlib.sha256((a.out/'selected_plans.tar.gz').read_bytes()).hexdigest(),
        scope='self-contained plan archive with unchanged verified scores and ordered hashes; cumulative library, not cold solves; extract archive beside CSV'))
    print(dict(configurations=len(rows), archive_bytes=(a.out/'selected_plans.tar.gz').stat().st_size))
