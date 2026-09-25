#!/usr/bin/env python3
"""Frozen, sequential Ascend C experiments. Run after sourcing CANN set_env.sh."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import time

FIELDS = ['id', 'mode', 'n', 'blocks', 'tile', 'buffers', 'rounds', 'groups', 'repeats']


def matrix(suite):
    rows = []

    def add(mode, n, blocks, tile=2048, buffers=2, rounds=1, groups=1, repeats=1):
        row = dict(zip(FIELDS[1:], [mode, n, blocks, tile, buffers, rounds, groups, repeats]))
        row['id'] = f'{suite}_{len(rows):03d}'
        rows.append(row)

    if suite == 'smoke':
        for b in [1, 2]:
            add('pipe', 1 << 18, 8, 2048, b, 16)
        for mode in ['barrier', 'branch']:
            add(mode, 1 << 18, 4, rounds=16, groups=4)
        for mode in ['fused', 'materialize']:
            add(mode, 1 << 18, 8, rounds=4, repeats=8)
        for mode in ['cache_adjacent', 'cache_roundrobin']:
            add(mode, 1 << 18, 8, groups=4, repeats=4)
    elif suite == 'pipe':
        for n in [1 << 20, 1 << 22]:
            for blocks in [1, 8, 32]:
                for tile in [512, 2048, 4096]:
                    for buffers in [1, 2]:
                        for rounds in [1, 16, 64]:
                            add('pipe', n, blocks, tile, buffers, rounds, repeats=4)
    elif suite == 'bandwidth':
        for blocks in [1, 2, 4, 8, 16, 32, 40, 48, 64]:
            for tile in [1024, 4096]:
                for rounds in [1, 16]:
                    add('pipe', 960 * 16384, blocks, tile, rounds=rounds, repeats=4)
    elif suite == 'barrier':
        for n in [1 << 18, 1 << 20]:
            for blocks in [1, 4, 16]:
                for groups in [2, 4, 8]:
                    for rounds in [16, 64]:
                        for mode in ['barrier', 'branch']:
                            add(mode, n, blocks, rounds=rounds, groups=groups)
    elif suite == 'reuse':
        for n in [1 << 14, 1 << 20, 1 << 24]:
            for blocks in [1, 32]:
                for stages in [2, 8, 32]:
                    for rounds in [1, 8]:
                        for mode in ['fused', 'materialize']:
                            add(mode, n, blocks, tile=512, rounds=rounds, repeats=stages)
    elif suite == 'sharing':
        for n in [31 << 18, 31 << 20]:
            for blocks in [1, 4, 16, 32]:
                for tile in [1024, 4096]:
                    for rounds in [1, 16]:
                        for mode in ['private', 'shared']:
                            add(mode, n, blocks, tile, rounds=rounds, repeats=4)
    elif suite == 'cache':
        for n in [1 << 16, 1 << 20, 1 << 22]:
            for blocks in [8, 32]:
                for groups in [1, 4, 16, 64]:
                    for mode in ['cache_adjacent', 'cache_roundrobin']:
                        add(mode, n, blocks, groups=groups, repeats=4)
    else:
        raise ValueError(suite)
    for row in rows:
        assert row['n'] % (row['blocks'] * row['tile']) == 0, row
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--suites', default='pipe,bandwidth,barrier,reuse,cache')
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--warmup', type=int, default=10)
    parser.add_argument('--binary', type=Path, default=Path(__file__).parent / 'build/mechanism_bench')
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).parent
    suites = args.suites.split(',')
    manifest = {
        'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'system': platform.platform(), 'samples': args.samples, 'warmup': args.warmup,
        'shuffle_seed': 20260926, 'suites': suites,
        'timing': 'ACL device event envelope, includes device scheduling and host submission gaps; NOT pure kernel time',
        'validation': 'full elementwise CPU reference before warmup and after EVERY timed sequence; outside timing',
        'cache_condition': 'no forced flush; configuration order shuffled each round; preceding validation copies may affect cache',
        'source_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()
                          if p.suffix in {'.cpp', '.py'} or p.name == 'CMakeLists.txt'},
        'binary_sha256': hashlib.sha256(args.binary.read_bytes()).hexdigest(),
    }
    # Freeze all configurations before the first measured batch.
    for suite in suites:
        with (out / f'{suite}_config.csv').open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(matrix(suite))
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    with (out / 'npu_before.txt').open('w') as f:
        subprocess.run(['npu-smi', 'info'], stdout=f, stderr=subprocess.STDOUT, timeout=15)
    for suite in suites:
        started = time.monotonic()
        print(f'START {suite} configs={len(matrix(suite))}', flush=True)
        with (out / f'{suite}.log').open('w') as log:
            proc = subprocess.run([str(args.binary.resolve()), str(out / f'{suite}_config.csv'),
                                   str(out / f'{suite}_raw.csv'), str(args.samples), str(args.warmup)],
                                  stdout=log, stderr=subprocess.STDOUT, timeout=2400)
        print(f'END {suite} returncode={proc.returncode} seconds={time.monotonic()-started:.2f}', flush=True)
        if proc.returncode:
            raise SystemExit(proc.returncode)
    with (out / 'npu_after.txt').open('w') as f:
        subprocess.run(['npu-smi', 'info'], stdout=f, stderr=subprocess.STDOUT, timeout=15)


if __name__ == '__main__':
    main()
