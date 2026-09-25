"""Restore portable official plans, preserving mapping insertion order and hashes."""
import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path


def unpack(bundles, out, ledger=None):
    out = out.resolve()
    expected = {}
    if ledger:
        with ledger.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                if row.get('plan_sha256'):
                    expected[row['plan']] = row['plan_sha256']
    restored = {}
    for source in bundles:
        with gzip.open(source, 'rt', encoding='utf-8') as stream:
            plans = json.load(stream)
        for name, plan in plans.items():
            target = (out/name).resolve()
            if not target.is_relative_to(out):
                raise ValueError('bundle path escapes output directory')
            raw = json.dumps(plan, ensure_ascii=False, allow_nan=False, separators=(',',':')).encode()
            digest = hashlib.sha256(raw).hexdigest()
            if name in expected and digest != expected[name]:
                raise ValueError('plan hash differs from official ledger: '+name)
            if name in restored and restored[name] != digest:
                raise ValueError('inconsistent duplicate plan: '+name)
            if target.exists() and target.read_bytes() != raw:
                raise FileExistsError('refuse to overwrite a different existing plan: '+str(target))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            restored[name] = digest
    if expected and set(expected) != set(restored):
        raise ValueError('bundle set does not cover the whole ledger')
    return dict(plans=len(restored), hashes_checked=bool(expected), output=str(out))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('bundles', type=Path, nargs='+')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--ledger', type=Path)
    a=p.parse_args()
    print(json.dumps(unpack(a.bundles, a.out, a.ledger), ensure_ascii=False))
