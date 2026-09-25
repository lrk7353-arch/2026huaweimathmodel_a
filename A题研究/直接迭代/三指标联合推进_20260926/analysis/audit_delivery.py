"""Check every portable delivered plan and ledger; no simulator calls."""
import csv
import math
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE.parent))
from common_run import DATA, GraphIR, read_json, atomic_json, validate_plan
from solver.common import object_digest


def main():
    ledger = list(csv.DictReader((HERE / '累计1500配置成绩.csv').open(encoding='utf-8-sig')))
    previous = list(csv.DictReader((HERE.parent / '联合整合_20260926/最终累计/累计1500配置成绩.csv').open(encoding='utf-8-sig')))
    key = lambda r: (r['case'], int(r['problem']), int(r['cores']))
    old = {key(r): r for r in previous}
    expected = {(f'case_{i:03d}', p, n) for i in range(1, 101) for p in (1, 2, 3) for n in range(1, 6)}
    assert len(ledger) == 1500 and {key(r) for r in ledger} == expected
    graphs = {}; improvements = []; source_counts = Counter()
    for r in ledger:
        k = key(r); before = old[k]
        if k[0] not in graphs:
            graphs[k[0]] = GraphIR.from_path(DATA / (k[0] + '.json'))
        plan = read_json(HERE / r['plan'])
        validate_plan(graphs[k[0]], plan)
        assert len(plan['core_schedules']) == k[2], k
        assert object_digest(plan) == r['plan_sha256'], k
        now_score = (int(r['makespan']), int(r['added_copy']))
        old_score = (int(before['makespan']), int(before['added_copy']))
        assert now_score <= old_score, k
        assert math.isclose(float(r['speedup']), int(r['original_singlecore']) / now_score[0], rel_tol=1e-12), k
        source_counts[r['source']] += 1
        if now_score < old_score:
            improvements.append(dict(case=k[0], problem=k[1], cores=k[2], before=list(old_score), after=list(now_score), source=r['source']))
    result = dict(plans=1500, unique_configurations=1500, valid_plans=1500, exact_ordered_hashes=1500,
                  regressions=0, improvements=improvements, source_counts=dict(source_counts), official_calls=0,
                  scope='Structural and ledger audit; official replay records separately verify newly accepted plans.')
    atomic_json(HERE / '交付审计.json', result)
    print(f'1500 plans valid; exact ordered hashes; zero regressions; {len(improvements)} improved configurations.')


if __name__ == '__main__':
    main()
