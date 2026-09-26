"""Read-only verification of the portable selected library; no evaluator calls."""
import csv
import hashlib
import json
import math
from pathlib import Path
import sys
import tarfile

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE.parent))
from common_run import DATA, GraphIR, validate_plan
from solver.common import object_digest


def main():
    package = HERE / '最终精选1500'
    manifest = json.loads((package / 'manifest.json').read_text())
    archive = package / 'selected_plans.tar.gz'
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest['archive_sha256']
    rows = list(csv.DictReader((package / '累计1500配置成绩.csv').open(encoding='utf-8-sig')))
    expected = {(f'case_{i:03d}', p, n) for i in range(1, 101) for p in (1, 2, 3) for n in range(1, 6)}
    assert len(rows) == 1500
    assert {(r['case'], int(r['problem']), int(r['cores'])) for r in rows} == expected
    by_name = {r['plan']: r for r in rows}
    assert len(by_name) == 1500
    seen, graphs = set(), {}
    with tarfile.open(archive, 'r|gz') as stream:
        for member in stream:
            assert member.isfile() and member.name in by_name and member.name not in seen
            r = by_name[member.name]
            plan = json.load(stream.extractfile(member))
            assert object_digest(plan) == r['plan_sha256']
            if r['case'] not in graphs:
                graphs[r['case']] = GraphIR.from_path(DATA / (r['case'] + '.json'))
            validate_plan(graphs[r['case']], plan)
            assert len(plan['core_schedules']) == int(r['cores'])
            assert math.isclose(float(r['speedup']), int(r['original_singlecore']) / int(r['makespan']), abs_tol=1e-10)
            seen.add(member.name)
    assert len(seen) == 1500
    for r in csv.DictReader((package / '题目口径核数曲线.csv').open(encoding='utf-8-sig')):
        group = [x for x in rows if (x['problem'], x['cores']) == (r['problem'], r['cores'])]
        expected_mean = 1.0 if r['cores'] == '1' else sum(float(x['speedup']) for x in group) / 100
        assert len(group) == 100 and math.isclose(float(r['mean_speedup']), expected_mean, abs_tol=1e-10)
    parts_dir = HERE / '正式v2完整审计'
    parts_manifest = json.loads((parts_dir / 'all_evaluated_plans.parts.json').read_text())
    joined = hashlib.sha256()
    for part in parts_manifest['parts']:
        data = (parts_dir / part['name']).read_bytes()
        assert len(data) == part['bytes'] and hashlib.sha256(data).hexdigest() == part['sha256']
        joined.update(data)
    assert joined.hexdigest() == parts_manifest['sha256']
    print('PASS: 1500 plan hashes, legal plans, core counts, score ratios, curves, and paid-plan archive parts.')
    print('This checks packaged evidence; it does not rerun the official simulator.')


if __name__ == '__main__':
    main()
