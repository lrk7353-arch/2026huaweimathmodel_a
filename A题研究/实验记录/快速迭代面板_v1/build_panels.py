#!/usr/bin/env python3
"""Freeze structural development panels without reading any evaluation result."""
import argparse
import bisect
import csv
from fractions import Fraction
import hashlib
import io
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent.parent
WORKSPACE = RESEARCH.parent
EXPECTED = tuple('case_{:03d}'.format(i) for i in range(1, 101))
FEATURES = (
    'compute_ops', 'tensors',
    'dependency_components', 'largest_pipe_component_fraction',
    'layer_fraction', 'dependency_edges_per_op',
    'work_M_fraction', 'count_M_fraction',
    'shared_input_byte_fraction', 'extra_component_read_fanout_bytes_ratio',
    'root_ddr_input_unique_bytes', 'original_copy_to_compute_5core_ratio',
)
GROUPS = {
    'scale': FEATURES[0:2], 'component_structure': FEATURES[2:4],
    'dependency_shape': FEATURES[4:6], 'M_V_mix': FEATURES[6:8],
    'input_reuse': FEATURES[8:10], 'input_and_traffic': FEATURES[10:12],
}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def unique(pairs):
    value = {}
    for k, v in pairs:
        require(k not in value, 'duplicate JSON key: ' + k)
        value[k] = v
    return value


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def ratio(a, b):
    return Fraction(a, b) if b else Fraction(0)


def input_snapshot(audit, data):
    names = ('a_graph_structure.csv', 'a_dependency_components.csv',
             'a_input_sharing.csv', 'a_structure_summary.json')
    sources = {name: sha(audit / name) for name in names}
    summary = read_json(audit / 'a_structure_summary.json')
    require(summary.get('case_count') == 100, 'structure audit is not full 100')
    expected = summary['input_sha256']
    require(set(expected) == {c + '.json' for c in EXPECTED}, 'audit graph hash scope differs')
    graph_hashes = {c: sha(data / (c + '.json')) for c in EXPECTED}
    require(all(graph_hashes[c] == expected[c + '.json'] for c in EXPECTED),
            'raw graph differs from the independently audited structural features')
    return {'structure_artifact_sha256': sources, 'graphs_sha256': graph_hashes,
            'config_sha256': sha(data / 'config.txt')}


def load_features(audit):
    original = read_csv(audit / 'a_graph_structure.csv')
    require(len(original) == 100 and {r['case'] for r in original} == set(EXPECTED),
            'structure CSV has duplicate/missing cases')
    graphs = {r['case']: r for r in original}
    components = {c: [] for c in EXPECTED}
    inputs = {c: [] for c in EXPECTED}
    for row in read_csv(audit / 'a_dependency_components.csv'):
        require(row['case'] in components, 'unknown component case')
        components[row['case']].append(row)
    for row in read_csv(audit / 'a_input_sharing.csv'):
        require(row['case'] in inputs, 'unknown input-sharing case')
        inputs[row['case']].append(row)
    result = {}
    for case in EXPECTED:
        g, cs, ins = graphs[case], components[case], inputs[case]
        n, nc = int(g['compute_ops']), int(g['dependency_components'])
        wm, wv = int(g['work_M']), int(g['work_V'])
        cm, cv = int(g['count_M']), int(g['count_V'])
        require(n > 0 and wm + wv > 0 and cm + cv == n and int(g['noncopy_other_pipe_ops']) == 0,
                'unsupported empty/other-pipe graph: ' + case)
        require(len(cs) == nc and len({r['component_id'] for r in cs}) == nc,
                'component count/id mismatch: ' + case)
        require(sum(int(r['ops']) for r in cs) == n and
                sum(int(r['work_M']) for r in cs) == wm and sum(int(r['work_V']) for r in cs) == wv,
                'component totals mismatch: ' + case)
        input_bytes = int(g['root_ddr_input_unique_bytes'])
        require(len(ins) == int(g['root_ddr_input_count']) and
                len({r['tensor_id'] for r in ins}) == len(ins) and
                sum(int(r['bytes']) for r in ins) == input_bytes,
                'input-sharing totals mismatch: ' + case)
        require(all(int(r['dependency_components']) >= 1 for r in ins), 'input has no compute component')
        max_m = max(int(r['work_M']) for r in cs)
        max_v = max(int(r['work_V']) for r in cs)
        shared_bytes = sum(int(r['bytes']) for r in ins if int(r['dependency_components']) > 1)
        extra_reads = sum(int(r['bytes']) * (int(r['dependency_components']) - 1) for r in ins)
        copy_bytes = int(g['original_copy_in_bytes']) + int(g['original_copy_out_bytes'])
        features = {
            'compute_ops': Fraction(n), 'tensors': Fraction(int(g['tensors'])),
            'dependency_components': Fraction(nc),
            'largest_pipe_component_fraction': max(ratio(max_m, wm), ratio(max_v, wv)),
            'layer_fraction': ratio(int(g['dependency_layers']), n),
            'dependency_edges_per_op': ratio(int(g['compute_dependency_edges']), n),
            'work_M_fraction': ratio(wm, wm + wv), 'count_M_fraction': ratio(cm, cm + cv),
            'shared_input_byte_fraction': ratio(shared_bytes, input_bytes),
            'extra_component_read_fanout_bytes_ratio': ratio(extra_reads, input_bytes),
            'root_ddr_input_unique_bytes': Fraction(input_bytes),
            # Original traffic / 60 divided by max(W_M,W_V)/5. Static diagnostic only.
            'original_copy_to_compute_5core_ratio': ratio(copy_bytes * 5, 60 * max(wm, wv)),
        }
        result[case] = {'case': case, 'features': features,
                        'largest_component_ops': max(int(r['ops']) for r in cs),
                        'largest_component_fraction': ratio(max(int(r['ops']) for r in cs), n),
                        'dependency_layers': int(g['dependency_layers']), 'work_M': wm, 'work_V': wv,
                        'original_copy_bytes': copy_bytes, 'shared_input_bytes': shared_bytes,
                        'max_input_component_fanout': max((int(r['dependency_components']) for r in ins), default=0)}
    return result


def rank_vectors(rows):
    """Twice zero-based tied midrank, in [0,198]; exact integer distances."""
    values = {k: sorted(r['features'][k] for r in rows.values()) for k in FEATURES}
    return {case: tuple(bisect.bisect_left(values[k], r['features'][k]) +
                        bisect.bisect_right(values[k], r['features'][k]) - 1 for k in FEATURES)
            for case, r in sorted(rows.items())}


def squared_distance(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b))


def farthest_cover(pool, count, vectors, *, anchors=(), first=None):
    """Medoid then farthest-first; disjoint extension uses prior panel as anchors."""
    pool, anchor_set = sorted(set(pool)), sorted(set(anchors))
    require(len(pool) >= count and not (set(pool) & set(anchor_set)), 'invalid/overlapping selection pool')
    selected, log = [], []
    if not anchor_set and count:
        pick = first if first is not None else min(pool, key=lambda c: (
            sum(squared_distance(vectors[c], vectors[x]) for x in pool), c))
        require(pick in pool, 'initial representative is outside selection pool')
        selected.append(pick)
        log.append({'case': pick, 'rule': 'largest_compute_ops_anchor' if first else 'minimum_total_squared_distance_medoid',
                    'distance_from_prior_set_squared_rank_units': None})
    while len(selected) < count:
        prior = anchor_set + selected
        available = [c for c in pool if c not in selected]
        distances = {c: min(squared_distance(vectors[c], vectors[p]) for p in prior) for c in available}
        pick = min(available, key=lambda c: (-distances[c], c))
        nearest = min(prior, key=lambda p: (squared_distance(vectors[p], vectors[pick]), p))
        selected.append(pick)
        log.append({'case': pick, 'rule': 'maximum_distance_to_previously_selected_structure',
                    'nearest_prior_case': nearest, 'distance_from_prior_set_squared_rank_units': distances[pick]})
    return selected, log


def select_panels(rows):
    vectors = rank_vectors(rows)
    quick = [c for c, r in rows.items() if r['features']['compute_ops'] <= 4000]
    development, development_log = farthest_cover(quick, 12, vectors)
    extension_pool = [c for c, r in rows.items() if r['features']['compute_ops'] <= 10000 and c not in development]
    extension, extension_log = farthest_cover(extension_pool, 24, vectors, anchors=development)
    pressure = sorted(c for c, r in rows.items() if r['features']['compute_ops'] > 10000)
    largest = min(pressure, key=lambda c: (-rows[c]['features']['compute_ops'], c))
    stress, stress_log = farthest_cover(pressure, 4, vectors, first=largest)
    return {'development12': development, 'extension24': extension, 'stress4': stress,
            'pressure_population': pressure, 'unselected_nonpressure': sorted(set(rows) - set(development + extension + pressure)),
            'pool_counts': {'development_eligible': len(quick), 'extension_eligible_after_development': len(extension_pool),
                            'pressure': len(pressure)},
            'selection_log': {'development12': development_log, 'extension24': extension_log, 'stress4': stress_log}}


def csv_text(rows, fields):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader(); writer.writerows(rows)
    return stream.getvalue()


def serializable_row(row):
    return {**{k: (float(v) if isinstance(v, Fraction) else v) for k, v in row.items() if k != 'features'},
            **{k: (v.numerator if v.denominator == 1 else float(v)) for k, v in row['features'].items()}}


def describe(row):
    f = row['features']
    return ('{n}计算节点；{nc}依赖分量，最大分量{lc:.1%}；M工作占比{m:.1%}；'
            '共享输入字节占比{share:.1%}；输入{mem:.2f} MiB；{depth}层').format(
        n=int(f['compute_ops']), nc=int(f['dependency_components']), lc=float(row['largest_component_fraction']),
        m=float(f['work_M_fraction']), share=float(f['shared_input_byte_fraction']),
        mem=float(f['root_ddr_input_unique_bytes']) / 1048576, depth=row['dependency_layers'])


def render_readme(panels, rows):
    lines = ['# 固定快速开发面板 v1', '',
             '本清单只用于开发筛选。此前 100 图均已进行结构查看或实验，三个面板都不是独立未见测试集。最终对外收益仍需全 100 图验证，不能把面板均值替代全量结果。', '',
             '## 固定规则', '',
             '- 开发面板：从计算节点 ≤4000 的55图选12图。',
             '- 扩展面板：从计算节点 ≤10000 且未进入开发面板的69图另选24图；与开发面板不重叠，合计36图。',
             '- 压力池：计算节点 >10000 的19图，默认快速循环不运行；提供4图压力子集，最大图为首个锚点。',
             '- 12项数值分为规模、分量、依赖形状、M/V混合、输入复用、输入与原始搬运六组；每组两项且等权。先转换为全100图并列中秩，再用精确整数平方欧氏距离。',
             '- 开发面板以候选池的平方距离medoid起步，再每次加入距离现有代表集合最远的图；扩展面板以固定开发12图为已有锚点继续覆盖剩余池；所有并列按case名称排序。没有随机数、收益排序或特定题号分支。',
             '- 压力子集只在压力池内做最远覆盖。压力池全部19图仍在清单中，4图不能代表完整压力验收。', '',
             '阈值是为了限制搜索与模拟的结构规模，不是实测运行时间承诺；周期数、spill和事件量仍会影响运行成本。构图/配置文件和结构CSV在读取前后均校验SHA256。未调用官方评估器，也未读取任何实验summary/record/胜负结果。', '',
             '## 特征解释与边界', '',
             '|组|两项选图特征|', '|---|---|']
    for key, names in GROUPS.items(): lines.append('|{}|{}|'.format(key, ' / '.join(names)))
    lines += ['', '`largest_pipe_component_fraction` 为 max(最大分量M工作/总M工作, 最大分量V工作/总V工作)，分母为0时该项取0。共享输入只指跨计算依赖分量共享的原始DDR输入；同分量内复用不计入这一项。', '',
              '`extra_component_read_fanout_bytes_ratio` 为 Σ[input_bytes×(触达分量数−1)]/Σinput_bytes；它是静态重复读潜力，不是实际新增COPY或缓存命中。`original_copy_to_compute_5core_ratio` 为 (原始COPY字节/60)/(max(M工作,V工作)/5)，只是原图静态代理，不包含切分、spill、FIFO、同步，也不证明模拟瓶颈。', '',
              '规模使用计算节点与张量数量；依赖形状使用层数/计算节点和依赖边/计算节点。层数不等于真实运行关键路径，平方距离也不构成最优代表性证明。', '']
    names = {'development12': '12图开发面板', 'extension24': '24图扩展面板（额外、不重叠）', 'stress4': '4图压力子集（单列）'}
    for key in names:
        lines += ['## ' + names[key], '', '|顺序|图|结构理由|', '|---|---|---|']
        for i, case in enumerate(panels[key], 1): lines.append('|{}|{}|{}|'.format(i, case, describe(rows[case])))
        lines += ['', 'CLI序号：`' + ','.join(str(int(c[5:])) for c in panels[key]) + '`', '']
    lines += ['## 全部压力池', '', ', '.join(panels['pressure_population']), '',
              '## 复现与使用', '',
              '在项目根目录执行（输出目录必须是新的，原选择不会被覆盖）：', '',
              '```bash', 'python3 -B A题研究/实验记录/快速迭代面板_v1/build_panels.py --output /tmp/a-panels-reproduction', '```', '',
              '`panels.json` 保存固定顺序、选图距离和精确规则；`all_features.csv` 保存全部100图诊断特征；`panel_table.csv` 保存所选40图的特征；三个 `.cases.txt` 文件提供可直接读取的case名称。`manifest.json` 包含100个原图、配置、结构文件、脚本以及产物的SHA256。', '',
              '建议先固定开发12图检查可行性、退化与机制，再一次运行额外24图决定是否扩大；压力图放在候选冻结后单独运行。通过面板只是进入全100图验证的筛选条件，不能作为获益结论。面板身份一经生成不随本轮结果换图。', '']
    return '\n'.join(lines)


def build(audit, data, output):
    raw_output = Path(output)
    require(not raw_output.is_symlink(), 'output cannot be a symlink')
    audit, data, output = Path(audit).resolve(), Path(data).resolve(), raw_output.resolve()
    require(not output.exists(), 'output must be new; do not overwrite a fixed panel')
    for protected in (audit, data.parent):
        require(output != protected and protected not in output.parents and output not in protected.parents,
                'output overlaps structural audit or original official attachment')
    source_before = sha(__file__)
    before = input_snapshot(audit, data)
    rows = load_features(audit)
    panels = select_panels(rows)
    repeated = select_panels(dict(reversed(list(rows.items()))))
    require(panels == repeated, 'selection is not deterministic under input row reordering')
    require(before == input_snapshot(audit, data) and source_before == sha(__file__), 'source/input changed during selection')
    require(len(set(panels['development12'] + panels['extension24'] + panels['stress4'])) == 40, 'overlapping panels')
    flat = [serializable_row(rows[c]) for c in EXPECTED]
    panel_rows = [dict(panel=key, panel_order=i, selection_rule=panels['selection_log'][key][i - 1]['rule'],
                       reason=describe(rows[c]), **serializable_row(rows[c]))
                  for key in ('development12', 'extension24', 'stress4') for i, c in enumerate(panels[key], 1)]
    panels.update({'schema_version': 1, 'selection_basis': 'structure_only_no_performance_results',
                   'independent_unseen_test_set': False, 'final_all100_validation_required': True,
                   'thresholds': {'development_compute_ops_max': 4000, 'extension_compute_ops_max': 10000,
                                  'pressure_compute_ops_min_exclusive': 10000},
                   'distance': 'sum of squared twice-zero-based-tied-midrank differences over 12 features; ranks fitted to all 100 graphs',
                   'feature_groups': GROUPS, 'tie_break': 'ascending case name', 'randomness': None})
    outputs = {'panels.json': json.dumps(panels, ensure_ascii=False, indent=2) + '\n',
               'all_features.csv': csv_text(flat, list(flat[0])),
               'panel_table.csv': csv_text(panel_rows, list(panel_rows[0])),
               'README.md': render_readme(panels, rows)}
    for key in ('development12', 'extension24', 'stress4'):
        outputs[key + '.cases.txt'] = '\n'.join(panels[key]) + '\n'
    manifest = {'schema_version': 1, 'scope': 'fixed development screening panels, not unseen test data',
                'official_evaluation_calls': 0, 'evaluation_result_files_read': 0,
                'source_unchanged_before_after': True, 'input_row_order_invariance_checked': True,
                'generator_sha256': source_before, **before,
                'source_locations': {'structure': 'A题研究/方案审阅/结构核验', 'data': '选题分析/A题附件/data'},
                'outputs_sha256': {name: hashlib.sha256(content.encode('utf-8')).hexdigest() for name, content in outputs.items()}}
    output.mkdir(parents=True, exist_ok=False)
    for name, content in outputs.items(): (output / name).write_text(content, encoding='utf-8')
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'output': str(output), 'development12': panels['development12'], 'extension24': panels['extension24'],
            'stress4': panels['stress4'], 'pressure_count': len(panels['pressure_population']),
            'official_evaluation_calls': 0, 'manifest_sha256': sha(output / 'manifest.json')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', type=Path, default=RESEARCH / '方案审阅/结构核验')
    parser.add_argument('--data', type=Path, default=WORKSPACE / '选题分析/A题附件/data')
    parser.add_argument('--output', type=Path, default=HERE / 'selection')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build(args.audit, args.data, args.output), ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
