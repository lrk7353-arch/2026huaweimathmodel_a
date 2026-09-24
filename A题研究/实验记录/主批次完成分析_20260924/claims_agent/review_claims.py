"""Read-only selected claim checks; never calls the official evaluator."""
from pathlib import Path
import collections
import gzip
import hashlib
import json

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
R = ROOT / 'A题研究'
FORMAL = R / 'advanced_solver/runs/formal_v2'
inputs = {}


def read(path):
    path = Path(path)
    raw = path.read_bytes()
    inputs[str(path.relative_to(ROOT))] = hashlib.sha256(raw).hexdigest()
    return json.loads(raw)


def official(record):
    path = Path(record['result_path'])
    raw = path.read_bytes()
    h = hashlib.sha256(raw).hexdigest()
    assert h == record['result_sha256']
    inputs[str(path.relative_to(ROOT))] = h
    result = json.loads(gzip.decompress(raw))
    assert result['makespan'] == record['metrics']['makespan']
    assert result['data_movement_bytes'] == record['metrics']['data_movement_bytes']
    return result


def interval_overlap(a, b):
    i = j = total = 0
    while i < len(a) and j < len(b):
        total += max(0, min(a[i][1], b[j][1]) - max(a[i][0], b[j][0]))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return total


def main():
    report = {'scope': 'read-only claim review, no new simulator executions; not a substitute for full main audit'}
    # Verify the exact assumptions needed by the original-DDR lower bound.
    bad = []
    stats = collections.Counter()
    for path in sorted((ROOT / '选题分析/A题附件/data').glob('case_*.json')):
        graph = read(path)
        ops = {o['id']: o for o in graph['ops']}
        for tensor in graph['tensors']:
            if set(tensor) != {'id', 'pos', 'size'}:
                bad.append([path.stem, 'nonstandard tensor fields may affect logical cache keys', tensor['id']])
        pred, succ = collections.defaultdict(list), collections.defaultdict(list)
        for edge in graph['edges']:
            succ[edge['source']].append(edge['target'])
            pred[edge['target']].append(edge['source'])
        ins, outs = [], []
        is_compute = lambda x: x in ops and ops[x]['op'] not in ('COPY_IN', 'COPY_OUT')
        for oid, op in ops.items():
            if op['op'] == 'COPY_IN':
                ins.extend(succ[oid])
                for tid in succ[oid]:
                    if not any(is_compute(x) for x in succ[tid]):
                        bad.append([path.stem, 'input not consumed by compute', oid, tid])
                    if any(is_compute(x) for x in pred[tid]):
                        bad.append([path.stem, 'input produced by compute', oid, tid])
            if op['op'] == 'COPY_OUT':
                outs.extend(pred[oid])
                for tid in pred[oid]:
                    if not any(is_compute(x) for x in pred[tid]):
                        bad.append([path.stem, 'output not produced by compute', oid, tid])
        for values, name in [(ins, 'input'), (outs, 'output')]:
            if len(set(values)) != len(values):
                bad.append([path.stem, 'repeated original ' + name + ' local tensor'])
        if set(ins) & set(outs):
            bad.append([path.stem, 'input/output overlap'])
        stats.update(graphs=1, all_tensors=len(graph['tensors']), original_input_tensors=len(ins), original_output_tensors=len(outs))
    report['original_DDR_bound_assumptions'] = {'counts': dict(stats), 'exceptions': bad}

    progress = read(FORMAL / 'component_p2_seed17/progress.json')
    rows = progress['slots']
    report['component_registered_coverage'] = {
        'reported_slots': progress['reported_slots'],
        'cases': sorted({x['case'] for x in rows}),
        'per_core': dict(collections.Counter(x['num_cores'] for x in rows)),
        'per_case': dict(sorted(collections.Counter(x['case'] for x in rows).items())),
        'scope_caution': 'prefix availability, not a randomized representative test set; compare only paired identical case/core settings',
    }

    count = collections.Counter()
    missing = []
    for case in range(1, 101):
        summaries = {p: read(FORMAL / f'full_p{p}_seed17/slots/case_{case:03d}/p{p}_n5/attempt_0001/summary.json') for p in (2, 3)}
        hashes = {p: summaries[p]['best']['plan_sha256'] for p in (2, 3)}
        count['same_plan' if hashes[2] == hashes[3] else 'different_plan'] += 1
        need = []
        for p, other in ((3, 2), (2, 3)):
            found = [x for x in summaries[p]['evaluations']
                     if x.get('plan_sha256') == hashes[other] and x.get('record', {}).get('status') == 'success']
            count['cross_found_in_same_case_trial' if found else 'cross_not_in_same_case_trial'] += 1
            if not found:
                need.append(f't{p}_pi{other}')
        if need:
            missing.append({'case': f'case_{case:03d}', 'cells': need})
    report['five_core_cross_cell_availability'] = {
        'counts': dict(count), 'not_in_pair_histories': missing,
        'scope_caution': 'availability only; existing records must still pass strict provenance/raw validation before reuse; other caches may contain more',
    }

    case = 'case_023'
    summary = read(FORMAL / f'full_p2_seed17/slots/{case}/p2_n5/attempt_0001/summary.json')
    baseline = read(R / f'solver/runs/full_initial_v1/results/{case}/singlecore.json')
    details = {}
    for name, record in [('official_singlecore', baseline), ('formal_p2_n5', summary['best']['record'])]:
        raw = official(record)
        core_rows = []
        for core in raw['per_core_timeline']:
            pipes = {p: sorted((op['start'], op['end']) for op in core['ops'] if op['pipe'] == p)
                     for p in ('PIPE_M', 'PIPE_V')}
            core_rows.append({'core': core['core_id'],
                              'compute_work': {p: sum(e - s for s, e in intervals) for p, intervals in pipes.items()},
                              'M_V_overlap_cycles': interval_overlap(pipes['PIPE_M'], pipes['PIPE_V'])})
        details[name] = {'makespan': raw['makespan'], 'data_movement_bytes': raw['data_movement_bytes'],
                         'core_rows': core_rows, 'sum_per_core_M_V_overlap_cycles': sum(x['M_V_overlap_cycles'] for x in core_rows),
                         'record_path': record['record_path'], 'raw_path': record['result_path']}
    report['superlinear_case_023'] = {'details': details,
        'speedup': baseline['metrics']['makespan'] / summary['best']['record']['metrics']['makespan'],
        'interpretation': 'Both have zero spill. Benefit includes changed same-core M/V overlap; do not attribute this case to reduced spill or treat N as an absolute speedup ceiling.'}

    four = read(R / '实验记录/P3四格_v2/summary.json')
    report['historical_four_cell_scope'] = {'scope': four['scope'],
        'rows': [{'case': x['case'], 'policy_status': x['policy_status'],
                  'cells': {k: v['metrics']['makespan'] for k, v in x['cells'].items()},
                  'ratios': x['ratios']} for x in four['cases']]}
    report['formal_four_cell_dispatcher_status'] = read(R / '实验记录/正式P3四格_v2/dependency_progress.json')
    report['input_sha256'] = dict(sorted(inputs.items()))
    (OUT / 'claims_evidence.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'original_DDR': report['original_DDR_bound_assumptions'],
                      'cross_availability': report['five_core_cross_cell_availability']['counts'],
                      'case_023_speedup': report['superlinear_case_023']['speedup']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
