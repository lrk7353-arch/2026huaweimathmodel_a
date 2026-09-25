"""Audit official evidence and safe pruning bounds without making solver calls."""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path

from common_run import DATA, R, GraphIR, atomic_json, read_json, validate_plan
from unified_structure import Structure
from unified_search import analytical_features


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            h.update(chunk)
    return h.hexdigest()


def validate_record(record, ir=None, raw=False):
    assert record['status'] == 'success', record.get('error')
    hashes = record['hashes']
    graph = DATA / Path(record['graph_path']).name
    assert sha(graph) == hashes['graph_sha256'], str(graph)
    assert sha(DATA/'config.txt') == hashes['config_sha256'], 'config changed'
    for name, digest in hashes['official_py_sha256'].items():
        assert sha(R.parent/'选题分析/A题附件/code'/name) == digest, name
    if record['problem']:
        assert sha(record['plan_path']) == hashes['plan_sha256'], record['plan_path']
        plan = read_json(record['plan_path'])
        if ir is not None:
            validate_plan(ir, plan)
        assert len(plan['core_schedules']) == record['metrics']['num_cores']
    assert sha(record['result_path']) == record['result_sha256'], record['result_path']
    if raw:
        with gzip.open(record['result_path'], 'rt', encoding='utf-8') as stream:
            result = json.load(stream)
        for key in ('makespan', 'data_movement_bytes', 'cache_stats', 'num_cores'):
            if key in result:
                assert result[key] == record['metrics'][key], key
    return True


def audit_bounds(roots, out):
    by_case = {}
    seen = set()
    for root in roots:
        for path in root.rglob('summary.json'):
            if 'source_snapshot' in path.parts:
                continue
            summary = read_json(path)
            for row in summary.get('evaluations', []):
                rec = row['record']
                if rec['status'] != 'success' or rec['problem'] not in (1, 2, 3):
                    continue
                identity = (rec['hashes']['graph_sha256'], rec['problem'], rec['hashes']['plan_sha256'])
                if identity not in seen:
                    seen.add(identity)
                    by_case.setdefault(Path(rec['graph_path']).stem, []).append(rec)
    checked, errors, pipes = [], [], Counter()
    for case, records in sorted(by_case.items()):
        ir = GraphIR.from_path(DATA/(case+'.json'))
        s = Structure(ir)
        pipes.update(ir.ops[o]['pipe'] for o in ir.compute_ids)
        for rec in records:
            try:
                plan = read_json(rec['plan_path'])
                validate_plan(ir, plan)
                f = analytical_features(s, plan, rec['problem'])
                actual = rec['metrics']['makespan']
                assert f['necessary_time'] <= actual + 1e-6, (f['necessary_time'], actual)
                checked.append(dict(case=case, problem=rec['problem'], plan_sha256=rec['hashes']['plan_sha256'],
                    necessary_time=f['necessary_time'], official_time=actual))
            except Exception as error:
                errors.append(dict(case=case, record=rec['record_path'], error=repr(error)))
        print(json.dumps(dict(case=case, audited=len(records), errors=len(errors))), flush=True)
    result = dict(checked=len(checked), graphs=len(by_case), pipes=dict(pipes), errors=errors,
                  checks=checked, official_calls=0)
    atomic_json(out, result)
    assert not errors, errors[:3]
    return {k:v for k,v in result.items() if k != 'checks'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('roots', type=Path, nargs='+')
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(audit_bounds(a.roots, a.out), ensure_ascii=False))
