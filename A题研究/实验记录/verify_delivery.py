#!/usr/bin/env python3
"""Portable, stdlib-only integrity verifier. Does not import/execute solver code.

Run from any working directory: python3 /path/to/package/verify_delivery.py
No original-machine path is read, and no official evaluation is performed.
"""
import argparse
import ast
from collections import Counter, defaultdict, deque
import gzip
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import sys

DATA = '选题分析/A题附件/data'
CODE = '选题分析/A题附件/code'
SOLVER = 'A题研究/solver'
REFINED = 'A题研究/精修求解器'
PORTFOLIO = 'A题研究/当前最佳方案'
GRAPH_NAMES = ['case_{:03d}'.format(i) for i in range(1, 101)]
EXPECTED_KEYS = {(c, p, n) for c in GRAPH_NAMES for p in (1, 2, 3) for n in range(1, 6)}
RAW_METRICS = ('makespan', 'num_cores', 'data_movement_bytes', 'cache_stats', 'memory_peak_by_core',
               'cross_task_traffic', 'task_count', 'capacity_bytes', 'bandwidth_bytes_per_cycle',
               'cache_capacity_bytes', 'cache_bandwidth_bytes_per_cycle')
FIXED_CONFIG = {'capacity': {'L1': 524288, 'UB': 131072}, 'bandwidth': {'bandwidth': 60},
                'multicore_scene_a': {'task_cross_core_wait_cycles': 1000, 'task_same_core_wait_cycles': 100},
                'multicore_scene_b': {'cross_core_copy_delay_cycles': 500},
                'problem_3': {'cache_capacity_bytes': 1048576, 'cache_bandwidth_bytes_per_cycle': 250}}


MINIMUM_RUNTIME = {
    'A题研究/solver': ['baselines.py', 'benchmark.py', 'common.py', 'eval_worker.py', 'evaluator.py', 'graph_ir.py', 'plan.py', 'solve.py', 'summarize.py'],
    'A题研究/advanced_solver': ['__init__.py', 'engine.py', 'solve.py', 'supervisor.py', 'operation_assign.py', 'component_baseline.py', 'trace_refine.py', 'cache_refine.py'],
    'A题研究/精修求解器': ['solve.py', 'controller.py', 'refine.py', 'p1_selective.py', 'wcc_interleave.py', 'wcc_interleave_v2.py', 'batch.py'],
    'A题研究/探索': ['advanced_solve.py', 'operation_heft_probe.py', 'partition_candidates.py'],
    '选题分析/A题附件/code': ['contest_io.py', 'evaluation_validation.py', 'multicore_cut_evaluate_problem_1.py',
                        'multicore_cut_evaluate_problem_2.py', 'multicore_cut_evaluate_problem_3.py',
                        'schedule_step1.py', 'schedule_step2.py', 'schedule_step3.py', 'singlecore_evaluate.py', 'stub_multicore_cut_and_schedule.py']}

def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def plan_hash(value):
    return hashlib.sha256(json_bytes(value)).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key: ' + key)
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('nonfinite JSON constant: ' + value)


def read(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream, object_pairs_hook=unique_object, parse_constant=reject_constant)


def safe_path(root, name, *, file=True):
    require(isinstance(name, str) and name and '\\' not in name and ':' not in name, 'invalid portable relative path')
    p = PurePosixPath(name)
    require(not p.is_absolute() and all(x not in ('.', '..') for x in p.parts) and str(p) == name, 'noncanonical or escaping path: ' + name)
    result = root.joinpath(*p.parts)
    require(root.resolve() in result.resolve().parents, 'path escapes package root: ' + name)
    for ancestor in [result] + list(result.parents):
        if ancestor == root:
            break
        require(not ancestor.is_symlink(), 'symlink forbidden in package: ' + name)
    if file:
        require(result.is_file(), 'missing package file: ' + name)
    return result


def parse_config(path):
    parsed, section = {}, None
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
            require(section not in parsed, 'duplicate config section')
            parsed[section] = {}
        else:
            fields = line.split()
            require(section is not None and len(fields) == 2 and fields[0] not in parsed[section], 'invalid config entry')
            parsed[section][fields[0]] = int(fields[1])
    require(parsed == FIXED_CONFIG, 'fixed official configuration differs')
    return parsed


def graph_view(graph):
    require(all(isinstance(graph.get(k), list) for k in ('ops', 'tensors', 'edges')), 'invalid graph schema')
    ops = {op['id']: op for op in graph['ops']}
    tensors = {t['id'] for t in graph['tensors']}
    require(len(ops) == len(graph['ops']) and len(tensors) == len(graph['tensors']) and not set(ops) & tensors, 'duplicate graph ids')
    require(all(type(i) is int and i >= 0 for i in set(ops) | tensors), 'noninteger graph id')
    producers, consumers = defaultdict(set), defaultdict(set)
    seen = set()
    for e in graph['edges']:
        a, b = e['source'], e['target']
        require((a, b) not in seen, 'duplicate graph edge')
        seen.add((a, b))
        if a in ops and b in tensors:
            producers[b].add(a)
        elif a in tensors and b in ops:
            consumers[a].add(b)
        else:
            raise ValueError('graph edge is not Op--Tensor bipartite')
    successors = {i: set() for i in ops}
    for t in tensors:
        for p in producers[t]:
            successors[p].update(consumers[t])
    degree = {i: 0 for i in ops}
    for targets in successors.values():
        for j in targets:
            degree[j] += 1
    queue = deque(i for i in ops if degree[i] == 0)
    topo = []
    while queue:
        i = queue.popleft(); topo.append(i)
        for j in successors[i]:
            degree[j] -= 1
            if degree[j] == 0: queue.append(j)
    require(len(topo) == len(ops), 'original graph has a cycle')
    compute = {i for i, op in ops.items() if op['op'] not in ('COPY_IN', 'COPY_OUT')}
    copy_reach = {}
    for i in reversed(topo):
        if i not in compute:
            reached = set()
            for j in successors[i]: reached.update((j,) if j in compute else copy_reach[j])
            copy_reach[i] = reached
    contracted = {}
    for i in compute:
        targets = set()
        for j in successors[i]: targets.update((j,) if j in compute else copy_reach[j])
        contracted[i] = targets
    return compute, contracted, ops


def verify_plan(plan, view, cores):
    require(isinstance(plan, dict) and set(plan) == {'node_to_subgraph', 'core_schedules'}, 'plan must have exactly two fields')
    raw, schedules = plan['node_to_subgraph'], plan['core_schedules']
    require(isinstance(raw, dict) and isinstance(schedules, list) and len(schedules) == cores, 'plan/core shape mismatch')
    mapping = {}
    for key, sg in raw.items():
        require(isinstance(key, str) and key.isascii() and key.isdecimal(), 'invalid original op ID')
        ident = int(key)
        require(ident not in mapping and type(sg) is int and sg >= 0, 'aliased op ID or invalid subgraph')
        mapping[ident] = sg
    require(set(mapping) == view[0], 'plan does not exactly cover original compute ops')
    listed = []
    for order in schedules:
        require(isinstance(order, list) and all(type(i) is int and i >= 0 for i in order), 'invalid core schedule')
        listed.extend(order)
    require(len(listed) == len(set(listed)) and set(listed) == set(mapping.values()), 'duplicate/missing scheduled subgraph')
    adjacent = {i: set() for i in listed}
    for i, children in view[1].items():
        for j in children:
            if mapping[i] != mapping[j]: adjacent[mapping[i]].add(mapping[j])
    for order in schedules:
        for i, j in zip(order, order[1:]): adjacent[i].add(j)
    degree = {i: 0 for i in adjacent}
    for children in adjacent.values():
        for j in children: degree[j] += 1
    ready = deque(i for i in degree if degree[i] == 0)
    count = 0
    while ready:
        i = ready.popleft(); count += 1
        for j in adjacent[i]:
            degree[j] -= 1
            if degree[j] == 0: ready.append(j)
    require(count == len(adjacent), 'subgraph/core-order cycle')
    return sum(bool(order) for order in schedules)


def finite_metrics(value):
    if isinstance(value, dict):
        for child in value.values(): finite_metrics(child)
    elif isinstance(value, list):
        for child in value: finite_metrics(child)
    elif isinstance(value, float):
        require(math.isfinite(value), 'nonfinite metric')


def timeline_binding(raw, view, problem, cores):
    """Condense raw schedule-to-plan evidence without keeping huge traces in RAM."""
    timelines = raw.get('per_core_timeline')
    require(isinstance(timelines, list) and len(timelines) == cores, 'raw timeline core coverage mismatch')
    require(all(type(c.get('core_id')) is int for c in timelines) and {c['core_id'] for c in timelines} == set(range(cores)), 'raw timeline core IDs mismatch')
    found, assignments, orders, finish = set(), [], [[] for _ in range(cores)], 0
    for core in timelines:
        cid = core['core_id']; tasks = core['tasks']
        orders[cid] = [t['subgraph_id'] for t in tasks] if problem == 1 else [sg for task in tasks for sg in task['subgraph_ids']]
        for op in core['ops']:
            start, end = op['start'], op['end']
            require(all(type(x) in (int, float) and math.isfinite(x) for x in (start, end)) and 0 <= start <= end, 'invalid raw timeline time')
            require(op.get('duration') == end - start, 'raw duration/start/end mismatch')
            finish = max(finish, end)
            ident = op['op_id']
            if ident not in view[0]:
                require(op['op'] in ('COPY_IN', 'COPY_OUT'), 'unknown inserted compute op in raw')
                continue
            original = view[2][ident]
            require(ident not in found and op['op'] == original['op'] and op['pipe'] == original['pipe'], 'raw original compute identity mismatch')
            require(end - start == max(1, original['cycles']), 'raw original compute duration mismatch')
            found.add(ident)
            sg = op['task_id'] if problem == 1 else op['subgraph_id']
            assignments.append([ident, sg, cid])
    require(found == view[0] and finish == raw['makespan'], 'raw compute coverage/final makespan mismatch')
    return plan_hash({'core_schedules': orders, 'assignment': sorted(assignments)})


def expected_binding(plan):
    owner = {sg: c for c, order in enumerate(plan['core_schedules']) for sg in order}
    return plan_hash({'core_schedules': plan['core_schedules'],
                      'assignment': sorted([int(op), sg, owner[sg]] for op, sg in plan['node_to_subgraph'].items())})


def raw_metadata(raw):
    fields = set(RAW_METRICS) | {'scene', 'problem', 'input_graph', 'input_plan', 'cache_mode', 'cross_core_copy_delay_cycles', 'task_cross_core_wait_cycles', 'task_same_core_wait_cycles'}
    return {k: raw[k] for k in fields if k in raw}


def verify_raw(raw, record, case, problem, cores, active, graph_hashes=None):
    require(record.get('status') == 'success' and type(record.get('problem')) is int and record.get('problem') == problem, 'record status/problem mismatch')
    require(type(record.get('returncode')) is int and record['returncode'] == 0, 'successful record has nonzero/missing exit code')
    m = record['metrics']; finite_metrics(m)
    require(set(m) <= set(RAW_METRICS) | {'active_cores', 'peak_memory_bytes'}, 'unknown record metric')
    require(raw.get('scene') == ('A' if problem == 1 else 'B'), 'raw scene mismatch')
    require(raw.get('problem') == 3 if problem == 3 else raw.get('problem') in (None, problem), 'raw problem mismatch')
    original_name = raw.get('input_graph')
    same_content_alias = (record.get('cache_hit') is True and graph_hashes is not None and isinstance(original_name, str)
                          and original_name.endswith('.json') and graph_hashes.get(original_name[:-5]) == graph_hashes[case])
    require(original_name == case + '.json' or same_content_alias, 'raw graph provenance mismatch')
    original_plan_name = record.get('source_input_plan_basename') or Path(record['plan_path']).name
    require(raw.get('input_plan') == original_plan_name, 'raw evaluator input-plan name mismatch')
    require(type(raw.get('num_cores')) is int and type(m.get('num_cores')) is int and raw.get('num_cores') == cores == m.get('num_cores'), 'raw core count mismatch')
    require(type(m.get('makespan')) in (int, float) and m['makespan'] > 0, 'invalid makespan')
    for field in RAW_METRICS:
        require(raw.get(field) == m.get(field), 'raw/record metric mismatch: ' + field)
    require(m.get('active_cores') == active, 'active cores mismatch')
    require(m.get('peak_memory_bytes') == record.get('peak_memory_bytes'), 'worker RSS record/metric mismatch')
    require(raw.get('capacity_bytes') == FIXED_CONFIG['capacity'] and raw.get('bandwidth_bytes_per_cycle') == 60, 'raw fixed capacity/bandwidth mismatch')
    if problem == 1:
        require(raw.get('task_cross_core_wait_cycles') == 1000 and raw.get('task_same_core_wait_cycles') == 100, 'raw P1 waits changed')
    else:
        require(raw.get('cross_core_copy_delay_cycles') == 500, 'raw cross-core delay changed')
    if problem == 3:
        require(raw.get('cache_mode') == 'read_only' and isinstance(raw.get('cache_stats'), dict), 'raw P3 cache mode mismatch')
        require(raw.get('cache_capacity_bytes') == 1048576 and raw.get('cache_bandwidth_bytes_per_cycle') == 250, 'raw P3 cache parameters changed')


def import_closure(root, code_paths):
    """Conservative static import check; no solver or official module is executed."""
    local_roots = {PurePosixPath(p).stem for p in code_paths}
    local_roots.update({'solver', 'advanced_solver', '精修求解器'})
    imports = set()
    for name in code_paths:
        tree = ast.parse(safe_path(root, name).read_text(encoding='utf-8'), filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import): imports.update(a.name.split('.')[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split('.')[0])
    unresolved = sorted(imports - local_roots - set(sys.stdlib_module_names) - {'__future__'})
    require(not unresolved, 'nonstdlib/unbundled imports: ' + ', '.join(unresolved))
    return sorted(imports)


def verify_delivery(directory, *, allow_partial=False, manifest_sha256=None):
    require(sys.version_info >= (3, 12), 'Python 3.12 or newer is required for this delivery verifier')
    root = Path(directory).resolve()
    manifest_path = safe_path(root, 'delivery_manifest.json')
    expected_sha = safe_path(root, 'MANIFEST.sha256').read_text(encoding='ascii').strip()
    require(sha(manifest_path) == expected_sha, 'manifest checksum mismatch')
    if manifest_sha256 is not None: require(expected_sha == manifest_sha256, 'trusted manifest hash mismatch')
    manifest = read(manifest_path)
    require(manifest.get('schema_version') == 1 and type(manifest.get('partial')) is bool, 'unsupported delivery manifest')
    require(allow_partial or not manifest['partial'], 'partial development snapshot; pass --allow-partial explicitly')
    inventory = manifest['inventory']
    require(isinstance(inventory, dict) and inventory, 'missing inventory')
    expected_files = set(inventory) | {'delivery_manifest.json', 'MANIFEST.sha256'}
    actual_files = set()
    for path in root.rglob('*'):
        require(not path.is_symlink(), 'symlink in package: ' + str(path.relative_to(root)))
        if path.is_file(): actual_files.add(path.relative_to(root).as_posix())
    require(actual_files == expected_files, 'missing or unlisted files: ' + repr(sorted(actual_files ^ expected_files)[:12]))
    for name, item in inventory.items():
        path = safe_path(root, name)
        require(path.stat().st_size == item['bytes'] and sha(path) == item['sha256'], 'inventory hash/size mismatch: ' + name)
    require(set(manifest['graphs_sha256']) == set(GRAPH_NAMES), 'must ship all 100 graph inputs even for partial plans')
    for case, value in manifest['graphs_sha256'].items():
        require(inventory[DATA + '/' + case + '.json']['sha256'] == value, 'graph inventory/manifest mismatch')
    require(inventory[DATA + '/config.txt']['sha256'] == manifest['config_sha256'], 'config manifest mismatch')
    parse_config(safe_path(root, DATA + '/config.txt'))
    code_paths = manifest['runtime_code_paths']
    require(len(code_paths) == len(set(code_paths)) and all(p in inventory for p in code_paths), 'runtime code inventory mismatch')
    for folder, names in MINIMUM_RUNTIME.items():
        for name in names:
            require(folder + '/' + name in code_paths, 'missing runtime/hash-manifest dependency: ' + folder + '/' + name)
    declared_official = {PurePosixPath(p).name: inventory[p]['sha256'] for p in code_paths if PurePosixPath(p).parent.as_posix() == CODE}
    require(declared_official == manifest['official_py_sha256'] and declared_official, 'official code mapping mismatch')
    imports = import_closure(root, code_paths)
    index = read(safe_path(root, manifest['index_path']))
    require(isinstance(index, list) and len(index) == manifest['selected_count'], 'index count mismatch')
    keys, views, raw_cache = set(), {}, {}
    for row in index:
        case, problem, cores = row['case'], row['problem'], row['num_cores']
        key = case, problem, cores
        require(type(problem) is int and type(cores) is int and key in EXPECTED_KEYS and key not in keys, 'duplicate/out-of-scope index slot')
        keys.add(key)
        expected_plan = PORTFOLIO + '/p{}/n{}/{}_multicore_res.json'.format(problem, cores, case)
        require(row['plan_path'] == expected_plan, 'noncanonical selected plan location')
        selected = read(safe_path(root, row['plan_path']))
        provenance = read(safe_path(root, row['provenance_path']))
        require(all(provenance[k] == row[k] for k in ('case', 'problem', 'num_cores', 'plan_path', 'plan_file_sha256', 'plan_sha256', 'makespan', 'added_copy_bytes', 'active_cores')), 'provenance/index mismatch')
        record = provenance['evaluation_record']; h = record['hashes']
        require(sha(safe_path(root, row['plan_path'])) == row['plan_file_sha256'], 'selected file hash mismatch')
        require(plan_hash(selected) == row['plan_sha256'] == h['plan_sha256'], 'selected evaluated-plan hash mismatch')
        require(record['graph_path'] == DATA + '/' + case + '.json' and record['config_path'] == DATA + '/config.txt' and record['official_code'] == CODE, 'nonportable input paths')
        require(h.get('problem') == problem and h['graph_sha256'] == manifest['graphs_sha256'][case] and h['config_sha256'] == manifest['config_sha256'], 'evaluation input/scenario hash mismatch')
        require(h['official_py_sha256'] == declared_official, 'evaluation official code mismatch')
        require(h['wrapper_sha256'] == inventory[SOLVER + '/evaluator.py']['sha256'] and h['worker_sha256'] == inventory[SOLVER + '/eval_worker.py']['sha256'], 'evaluation wrapper/worker mismatch')
        require(sha(safe_path(root, record['plan_path'])) == h['plan_sha256'], 'exact evaluated input bytes mismatch')
        require(plan_hash(read(safe_path(root, record['plan_path']))) == h['plan_sha256'], 'evaluated plan object mismatch')
        if case not in views: views[case] = graph_view(read(safe_path(root, record['graph_path'])))
        active = verify_plan(selected, views[case], cores)
        raw_path = safe_path(root, record['result_path'])
        require(inventory[record['result_path']]['sha256'] == record['result_sha256'], 'original gzip SHA mismatch')
        if record['result_sha256'] not in raw_cache:
            with gzip.open(raw_path, 'rt', encoding='utf-8') as stream:
                raw = json.load(stream, object_pairs_hook=unique_object, parse_constant=reject_constant)
            raw_cache[record['result_sha256']] = {'metadata': raw_metadata(raw), 'binding': timeline_binding(raw, views[case], problem, cores)}
        cached = raw_cache[record['result_sha256']]
        require(cached['binding'] == expected_binding(selected), 'raw timeline does not match exact selected assignment/core order')
        verify_raw(cached['metadata'], record, case, problem, cores, active, manifest['graphs_sha256'])
        m = record['metrics']
        require(row['makespan'] == m['makespan'] and row['added_copy_bytes'] == m['data_movement_bytes']['added_copy_bytes'] and row['active_cores'] == active, 'catalog objective mismatch')
    missing = sorted(EXPECTED_KEYS - keys)
    require(manifest['partial'] == bool(missing), 'partial/full flag inconsistent with exact coverage')
    require(manifest['missing_slots'] == [list(x) for x in missing], 'missing-slot declaration mismatch')
    require(manifest['expected_full_coverage'] == 1500 and manifest['source_unchanged_before_after'] is True, 'coverage/source invariant missing')
    return {'status': 'verified_partial' if missing else 'verified_complete', 'selected_count': len(keys),
            'expected_full_coverage': 1500, 'missing_count': len(missing), 'inventory_files': len(inventory),
            'unique_original_gzip_verified': len(raw_cache), 'manifest_sha256': expected_sha,
            'static_import_roots_verified': imports, 'official_evaluations_performed': 0,
            'scope': 'integrity and archived official evidence; not fresh replay, equal-budget comparison, or proof of global optimality'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', nargs='?', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--allow-partial', action='store_true')
    parser.add_argument('--manifest-sha256', help='Optional independently saved trusted manifest SHA256')
    args = parser.parse_args(argv)
    try:
        result = verify_delivery(args.package, allow_partial=args.allow_partial, manifest_sha256=args.manifest_sha256)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'verification_failed', 'error': '{}: {}'.format(type(error).__name__, error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
