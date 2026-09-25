#!/usr/bin/env python3
"""Verify an exported hardware experiment bundle without needing an NPU."""
import argparse
from collections import defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import tarfile


def require(condition, message):
    if not condition:
        raise ValueError(message)


def source_members(path):
    with tarfile.open(path, 'r:gz') as archive:
        return {m.name: hashlib.sha256(archive.extractfile(m).read()).hexdigest()
                for m in archive.getmembers() if m.isfile()}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('root', type=Path)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    root = args.root.resolve()
    manifest = json.loads((root / 'artifact_manifest.json').read_text())
    for name, meta in manifest['files'].items():
        path = (root / name).resolve()
        require(root in path.parents, f'Path outside bundle: {name}')
        data = path.read_bytes()
        require(len(data) == meta['bytes'], f'Wrong size: {name}')
        require(hashlib.sha256(data).hexdigest() == meta['sha256'], f'Wrong hash: {name}')
    batches = {}
    for batch, expected in manifest['formal_batches'].items():
        groups = defaultdict(dict)
        directory = root / batch
        files = sorted(directory.glob('*_raw.csv')) or [directory / 'raw.csv']
        for path in files:
            for row in csv.DictReader(path.open()):
                key = row.get('id') or (row['size'], row['elements'], row['mode'])
                sample = int(row['sample'])
                require(sample not in groups[key], f'Duplicate sample: {batch}, {key}, {sample}')
                for field in ['device_envelope_us', 'host_us']:
                    value = float(row[field])
                    require(math.isfinite(value) and value > 0, f'Bad timing: {batch}/{key}/{field}')
                require(('max_abs_error' in row and float(row['max_abs_error']) == 0)
                        or row.get('correct') == 'True', f'Incorrect output: {batch}/{key}')
                groups[key][sample] = float(row['device_envelope_us'])
        require(all(set(values) == set(range(30)) for values in groups.values()),
                f'Expected samples 0..29 in every configuration: {batch}')
        configs = len(groups)
        samples = sum(len(values) for values in groups.values())
        require(configs == expected['configuration_batches'], f'Config count mismatch: {batch}')
        require(samples == expected['timed_sequences'], f'Sample count mismatch: {batch}')
        # Independently reproduce reported medians from the raw rows.
        for row in csv.DictReader((directory / 'summary.csv').open()):
            if 'id' in row:
                pairs = [(row['id'], float(row['median_us']))]
            else:
                pairs = [((row['size'], row['elements'], mode), float(row[mode + '_median_us']))
                         for mode in ['matrix_only', 'vector_only', 'serial', 'parallel']]
            for key, reported in pairs:
                actual = statistics.median(groups[key].values())
                require(math.isclose(actual, reported, rel_tol=1e-10, abs_tol=1e-7),
                        f'Summary median mismatch: {batch}/{key}')
        batches[batch] = {'configurations': configs, 'samples': samples, 'all_correct': True}
    initial = source_members(root / 'source_snapshots/initial.tar.gz')
    expanded = source_members(root / 'source_snapshots/expanded.tar.gz')
    source_checks = []
    for batch, members, names in [
            ('full_01', initial, ['main.cpp', 'vector_probe.cpp', 'run_suite.py', 'CMakeLists.txt']),
            ('sharing_01', expanded, ['main.cpp', 'vector_probe.cpp', 'sharing_probe.cpp', 'run_suite.py', 'CMakeLists.txt'])]:
        hashes = json.loads((root / batch / 'manifest.json').read_text())['source_sha256']
        for name in names:
            require(members['mechanisms/' + name] == hashes[name], f'Source mismatch: {batch}/{name}')
            source_checks.append(f'{batch}/{name}')
    for batch, members in [('mixed_01', initial), ('mixed_graph_01', expanded)]:
        expected = json.loads((root / batch / 'manifest.json').read_text())['source_sha256']
        require(members['mixed_resources.py'] == expected, f'Mixed source mismatch: {batch}')
        source_checks.append(f'{batch}/mixed_resources.py')
    total = sum(v['samples'] for v in batches.values())
    require(total == manifest['formal_timed_sequences'] == 14640, 'Formal total mismatch')
    report = {'passed': True, 'hashed_files': len(manifest['files']), 'formal_batches': batches,
              'formal_configuration_batches': sum(v['configurations'] for v in batches.values()),
              'formal_timed_sequences': total, 'source_checks': source_checks,
              'checks': ['all exported file hashes and sizes', 'all timed output correctness flags',
                         'positive finite timings', '30 unique samples per configuration',
                         'summary medians reproduced from raw data', 'exact executed core source snapshots'],
              'scope': 'Artifact integrity and internal consistency; does not prove physical independence or contest gains.'}
    text = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(text)
    print(text)


if __name__ == '__main__':
    main()
