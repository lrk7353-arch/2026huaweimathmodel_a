#!/usr/bin/env python3
"""Build a self-contained, archived-evidence delivery; never runs official eval.

Default requires all 1500 case/problem/core slots. --allow-partial is an explicit
snapshot, not a complete delivery. Source files are copied byte-for-byte and
checked again before publication. Existing output directories are never reused.
"""
import argparse
import copy
import csv
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[1]
sys.path.insert(0, str(HERE))
import verify_delivery as v

MINIMUM_RUNTIME = v.MINIMUM_RUNTIME


def save(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def output_guard(output, portfolio, workspace):
    raw = Path(output).expanduser()
    v.require(not raw.exists() and not raw.is_symlink(), 'strictly new output directory required; existing empty directories also rejected')
    out = raw.resolve()
    v.require(not out.exists(), 'resolved output already exists')
    protected = [portfolio, workspace / '选题分析/A题附件']
    protected += [workspace / name for name in MINIMUM_RUNTIME]
    for source in protected:
        source = source.resolve()
        v.require(out != source and source not in out.parents and out not in source.parents, 'output overlaps protected source: ' + str(source))
    return out


def runtime_files(workspace):
    paths = set()
    for folder, required in MINIMUM_RUNTIME.items():
        for name in required:
            v.require((workspace / folder / name).is_file(), 'runtime missing/not yet frozen: ' + folder + '/' + name)
        # Explicit frozen closure only. New research .py files in these folders
        # are deliberately not swept into a reproducible delivery by a glob.
        paths.update(workspace / folder / name for name in required)
    return sorted(paths)


def load_catalog(portfolio, *, allow_partial):
    with (portfolio / 'catalog.csv').open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    keys = set()
    for row in rows:
        case, p, n = row['case'], int(row['problem']), int(row['num_cores'])
        key = case, p, n
        v.require(key in v.EXPECTED_KEYS and key not in keys, 'duplicate/out-of-scope catalog slot: ' + repr(key))
        keys.add(key)
    missing = sorted(v.EXPECTED_KEYS - keys)
    v.require(allow_partial or not missing, 'incomplete portfolio: {} / 1500 slots; explicit --allow-partial needed'.format(len(keys)))
    manifest = v.read(portfolio / 'manifest.json')
    v.require(manifest['selected_count'] == len(rows), 'portfolio manifest/catalog count mismatch')
    v.require(not manifest.get('rejected'), 'portfolio contains rejected-source diagnostics; rebuild a clean verified portfolio')
    actual = {p.resolve() for p in portfolio.glob('p*/n*/case_*_multicore_res.json')}
    expected = {portfolio / 'p{}'.format(p) / 'n{}'.format(n) / (c + '_multicore_res.json') for c, p, n in keys}
    v.require(actual == expected, 'selected plan files missing, duplicated or unindexed')
    return sorted(rows, key=lambda r: (r['case'], int(r['problem']), int(r['num_cores']))), missing, manifest


def compact_record(record, *, case, plan_path, result_path):
    keep = ('schema_version', 'attempt_id', 'problem', 'status', 'metrics', 'elapsed_seconds',
            'evaluation_elapsed_seconds', 'worker_elapsed_seconds', 'cache_hit', 'source_attempt_id',
            'result_sha256', 'error', 'returncode', 'peak_memory_bytes', 'rss_measurement', 'timeout_seconds')
    portable = {k: copy.deepcopy(record[k]) for k in keep if k in record}
    portable['hashes'] = {k: copy.deepcopy(record['hashes'][k]) for k in ('graph_sha256', 'plan_sha256', 'problem',
                                  'config_sha256', 'official_py_sha256', 'wrapper_sha256', 'worker_sha256')}
    portable.update(graph_path=v.DATA + '/' + case + '.json', config_path=v.DATA + '/config.txt',
                    official_code=v.CODE, plan_path=plan_path, result_path=result_path)
    portable['source_input_plan_basename'] = Path(record['plan_path']).name
    portable['runtime_at_evaluation'] = {k: record['hashes'].get(k) for k in ('python', 'platform')}
    portable['normalization'] = 'portable path/index subset of original record; original gzip bytes unchanged; historical cache not restored'
    return portable


def origin_label(origin, workspace):
    path = Path(origin)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(workspace).as_posix()
        except ValueError:
            return 'external_source/' + path.name
    return str(origin).replace('\\', '/')


def publish_new(stage, output):
    """Reserve the destination atomically; never replace a racing empty dir.

    The checksum is moved last. An interrupted publication has no valid complete
    package, remains visibly incomplete, and must not be reused on the next run.
    """
    output.mkdir()  # Atomic exclusive reservation; also rejects dangling symlinks.
    try:
        children = sorted(stage.iterdir(), key=lambda p: (p.name in ('delivery_manifest.json', 'MANIFEST.sha256'), p.name == 'MANIFEST.sha256', p.name))
        for source in children:
            destination = output / source.name
            v.require(not destination.exists() and not destination.is_symlink(), 'unexpected content appeared in reserved output')
            os.rename(source, destination)
        stage.rmdir()
    except Exception as error:
        failure = output / 'INCOMPLETE_DELIVERY.json'
        with failure.open('x', encoding='utf-8') as handle:
            json.dump({'status': 'incomplete_publication', 'error': str(error),
                       'instruction': 'Do not treat as a delivery; choose a new output directory for a retry.'}, handle)
        raise


def readme(partial, selected_count):
    return '''# A 题计算交付包

状态：{state}，包含 {count} / 1500 份已验证方案。完整范围为正式100图 × P1/P2/P3 × 1–5核；缺失项逐条列在 delivery_manifest.json。任何 partial 包都不能作为完整交付。

这是已保存实验中的当前最好方案组合，先比较 makespan、相同再比较新增 COPY。它不代表同预算算法成绩，也不证明全局最优。官方结果是归档证据；本包验真不重新运行官方评估器。

## 独立验真

要求 Python 3.12 或更新版本，仅使用标准库。从任意工作目录执行（把路径替换为解压后的包路径）：

```sh
python3 -B /path/to/package/verify_delivery.py{partial_flag}
```

完整验真逐文件检查 SHA256，再核对方案覆盖/合法结构、原图与配置、官方源码、场景与核数、精确评估计划 hash、原始 gzip 和全部记录指标。可以另传 `--manifest-sha256 <独立保存的摘要>` 校验可信 manifest；包内自带摘要用于完整性检测，不是数字签名。不要修改或往包内添加文件；新实验输出请放在包外。

## 新精修入口

保留 A题研究/ 与 选题分析/ 的相对层级。下列命令从交付包根目录执行；先查看已打包入口的实际参数：

```sh
python3 -B A题研究/精修求解器/solve.py --help
python3 -B A题研究/精修求解器/solve.py 选题分析/A题附件/data/case_071.json -p 3 -n 5 --run-dir ../A题复跑/case071_p3_n5 -o ../A题复跑/case071_p3_n5.plan.json
```

正式参数来自包内原版 data/config.txt：L1 524288、UB 131072、DDR带宽60，P1跨核/同核等待1000/100，P2/P3跨核COPY延迟500，P3只读FIFO缓存容量1048576、带宽250。源码与数据保持原始字节，不修改配置。

官方模拟器和本求解器在 CPU 上运行，不需要 GPU。运行耗时取决于图大小与搜索预算。建议使用与已有验证一致的 Python3.12；尚未声称在每个操作系统验证过。备用 supervisor 有系统相关行为，本说明的主入口是新 solve.py。

## 文件和边界

- delivery_index.json：每个选中方案、对应 provenance 和指标的相对目录索引。
- A题研究/当前最佳方案/p*/n*/：可提交的两字段方案与便携 provenance；evidence/ 保留精确评估输入计划，official_results/ 保留去重后的原始官方gzip。
- A题研究/solver、advanced_solver、精修求解器和探索三个依赖：运行代码。探索旧脚本只作为依赖打包，不承诺它们依赖旧实验档案的历史CLI可直接复跑。
- 选题分析/A题附件/code 与 data：原官方代码、全部100图与固定配置。
- 源码/输入在复制前后均校验哈希。旧缓存含解释器路径和平台身份，未作为搬迁后的可恢复缓存打包；新运行会在包外建立自己的日志和缓存。
- 未打包依赖旧归档fixture的项目测试，也不声称它们在本包中运行通过。verify_delivery.py 是本包独立验真工具，不执行求解器代码。
- 若包含 source_snapshot/，其原始本地路径仅是可选追踪材料，任何执行命令与验真都不依赖那些路径。

论文和排版材料不在本交付包中。
'''.format(state='PARTIAL 开发快照' if partial else '完整计算交付', count=selected_count,
           partial_flag=' --allow-partial' if partial else '')


def build_delivery(portfolio, output, *, allow_partial=False, snapshot=False, workspace=WORKSPACE):
    v.require(sys.version_info >= (3, 12), 'Python 3.12 or newer is required for packaging')
    workspace, portfolio = Path(workspace).resolve(), Path(portfolio).resolve()
    v.require(portfolio.is_dir(), 'portfolio directory missing')
    out = output_guard(output, portfolio, workspace)
    tracked = {}

    def remember(path):
        path = Path(path).resolve()
        v.require(path.is_file(), 'source file missing: ' + str(path))
        value = v.sha(path)
        v.require(path not in tracked or tracked[path] == value, 'source changed while packaging: ' + str(path))
        tracked[path] = value
        return value

    # Coverage errors must be rejected before copying or creating output artifacts.
    remember(portfolio / 'catalog.csv'); remember(portfolio / 'manifest.json')
    rows, missing, original_manifest = load_catalog(portfolio, allow_partial=allow_partial)
    runtime = runtime_files(workspace)
    for path in runtime: remember(path)
    graphs = {c: workspace / v.DATA / (c + '.json') for c in v.GRAPH_NAMES}
    graph_hashes = {c: remember(path) for c, path in graphs.items()}
    config = workspace / v.DATA / 'config.txt'; config_sha = remember(config)
    v.parse_config(config)
    official_hashes = {p.name: tracked[p] for p in runtime if p.parent == workspace / v.CODE}
    v.require(original_manifest['source_hashes'] == official_hashes and original_manifest['config_sha256'] == config_sha, 'portfolio official source/config mismatch')
    remember(Path(__file__)); remember(Path(v.__file__))
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.' + out.name + '.building-', dir=str(out.parent)))
    published = False
    copied, index, views, raw_cache = {}, [], {}, {}

    def copy_file(source, relative):
        source = Path(source).resolve(); value = remember(source)
        target = v.safe_path(stage, relative, file=False)
        if relative in copied:
            v.require(copied[relative] == value, 'two sources collide in package: ' + relative)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        v.require(v.sha(target) == value, 'source changed during byte copy: ' + str(source))
        copied[relative] = value

    try:
        for path in runtime: copy_file(path, path.relative_to(workspace).as_posix())
        for path in graphs.values(): copy_file(path, path.relative_to(workspace).as_posix())
        copy_file(config, v.DATA + '/config.txt')
        copy_file(Path(v.__file__), 'verify_delivery.py')
        if snapshot:
            copy_file(portfolio/'catalog.csv', 'source_snapshot/original_catalog.csv')
            copy_file(portfolio/'manifest.json', 'source_snapshot/original_portfolio_manifest.json')
        for catalog in rows:
            case, problem, cores = catalog['case'], int(catalog['problem']), int(catalog['num_cores'])
            rel_plan = v.PORTFOLIO + '/p{}/n{}/{}_multicore_res.json'.format(problem, cores, case)
            selected_path = portfolio / 'p{}'.format(problem) / 'n{}'.format(cores) / (case + '_multicore_res.json')
            declared = Path(catalog['plan_path'])
            declared = declared if declared.is_absolute() else workspace / declared
            v.require(declared.resolve() == selected_path.resolve(), 'catalog plan path does not match its canonical slot')
            plan_file_sha = remember(selected_path)
            v.require(catalog['plan_sha256'] == plan_file_sha, 'catalog selected file SHA mismatch')
            original_prov_path = selected_path.with_suffix('.provenance.json')
            prov_sha = remember(original_prov_path)
            prov = v.read(original_prov_path); plan = v.read(selected_path); record = prov['evaluation_record']
            v.require(all(str(prov[k]) == str(catalog[k]) for k in ('case', 'problem', 'num_cores', 'makespan', 'added_copy_bytes', 'active_cores', 'plan_path', 'plan_sha256', 'official_result_path')), 'catalog/source provenance disagreement')
            h = record['hashes']
            v.require(record['status'] == 'success' and record['problem'] == h['problem'] == problem, 'record scenario/status mismatch')
            v.require(h['graph_sha256'] == graph_hashes[case] and h['config_sha256'] == config_sha and h['official_py_sha256'] == official_hashes, 'record source/input hash mismatch')
            v.require(h['wrapper_sha256'] == tracked[workspace/v.SOLVER/'evaluator.py'] and h['worker_sha256'] == tracked[workspace/v.SOLVER/'eval_worker.py'], 'record wrapper/worker version differs from bundled source')
            v.require(v.plan_hash(plan) == h['plan_sha256'], 'selected plan differs from official evaluated order/content')
            original_eval_plan = Path(record['plan_path'])
            v.require(remember(original_eval_plan) == h['plan_sha256'] and v.plan_hash(v.read(original_eval_plan)) == h['plan_sha256'], 'original evaluator input plan differs')
            record_path = Path(record['record_path']); record_sha = remember(record_path)
            v.require(v.read(record_path) == record, 'original record JSON differs from provenance')
            original_result = Path(record['result_path'])
            v.require(str(original_result) == catalog['official_result_path'] and remember(original_result) == record['result_sha256'], 'original gzip path/hash mismatch')
            if case not in views: views[case] = v.graph_view(v.read(graphs[case]))
            active = v.verify_plan(plan, views[case], cores)
            if record['result_sha256'] not in raw_cache:
                with gzip.open(original_result, 'rt', encoding='utf-8') as stream:
                    raw = json.load(stream, object_pairs_hook=v.unique_object, parse_constant=v.reject_constant)
                raw_cache[record['result_sha256']] = {'metadata': v.raw_metadata(raw), 'binding': v.timeline_binding(raw, views[case], problem, cores)}
            cached = raw_cache[record['result_sha256']]
            v.require(cached['binding'] == v.expected_binding(plan), 'original gzip timeline does not match selected plan')
            v.verify_raw(cached['metadata'], record, case, problem, cores, active, graph_hashes)
            m = record['metrics']
            v.require(float(catalog['makespan']) == m['makespan'] and int(catalog['added_copy_bytes']) == m['data_movement_bytes']['added_copy_bytes'] and int(catalog['active_cores']) == active, 'catalog metrics differ from exact official evidence')
            exact_path = v.PORTFOLIO + '/evidence/p{}_n{}_{}.evaluated.plan.json'.format(problem, cores, case)
            result_path = v.PORTFOLIO + '/official_results/' + record['result_sha256'] + '.json.gz'
            prov_path = str(PurePath(rel_plan).with_suffix('.provenance.json'))
            copy_file(selected_path, rel_plan); copy_file(original_eval_plan, exact_path); copy_file(original_result, result_path)
            row = {'case': case, 'problem': problem, 'num_cores': cores, 'makespan': m['makespan'],
                   'added_copy_bytes': m['data_movement_bytes']['added_copy_bytes'], 'active_cores': active,
                   'plan_path': rel_plan, 'plan_file_sha256': plan_file_sha, 'plan_sha256': h['plan_sha256'], 'provenance_path': prov_path}
            portable = {**row, 'evaluation_record': compact_record(record, case=case, plan_path=exact_path, result_path=result_path),
                        'source_provenance_sha256': prov_sha, 'source_record_sha256': record_sha,
                        'historical_origin_label_not_a_package_dependency': origin_label(catalog['origin'], workspace),
                        'selection_scope': 'best known saved portfolio, not an equal-budget algorithm or global optimum'}
            save(stage / prov_path, portable)
            if snapshot:
                suffix = 'p{}_n{}_{}'.format(problem, cores, case)
                copy_file(original_prov_path, 'source_snapshot/' + suffix + '.original.provenance.json')
                copy_file(record_path, 'source_snapshot/' + suffix + '.original.record.json')
            index.append(row)
        save(stage/'delivery_index.json', index)
        (stage/'README.md').write_text(readme(bool(missing), len(index)), encoding='utf-8')
        # Documentation is captured last; changes while bulk evidence is copied
        # do not force a rebuild. Each copied snapshot is still hash-verified.
        document_snapshots = []
        for name in ('SOLVE.md', 'REFINE.md', 'README_wcc_interleave.md', 'README_wcc_interleave_v2.md'):
            document = workspace / v.REFINED / name
            if document.is_file():
                for attempt in range(3):
                    before = v.sha(document); content = document.read_bytes()
                    if before == v.sha(document) == v.hashlib.sha256(content).hexdigest():
                        relative = v.REFINED + '/' + name
                        target = stage / relative; target.write_bytes(content)
                        document_snapshots.append({'path': relative, 'sha256': before,
                            'captured_at': datetime.now(timezone.utc).isoformat(),
                            'scope': 'documentation snapshot; later editorial changes are outside the frozen code/input check'})
                        break
                else:
                    raise ValueError('documentation changed repeatedly during capture: ' + name)
        # No source files, including selected plans and raw evidence, changed during copying.
        for path, value in tracked.items(): v.require(v.sha(path) == value, 'source changed before publication: ' + str(path))
        runtime_paths = [p.relative_to(workspace).as_posix() for p in runtime]
        v.import_closure(stage, runtime_paths)
        inventory = {p.relative_to(stage).as_posix(): {'sha256': v.sha(p), 'bytes': p.stat().st_size}
                     for p in sorted(stage.rglob('*')) if p.is_file()}
        manifest = {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'partial': bool(missing), 'selected_count': len(index), 'expected_full_coverage': 1500,
                    'missing_slots': [list(key) for key in missing], 'index_path': 'delivery_index.json',
                    'inventory': inventory, 'runtime_code_paths': runtime_paths, 'graphs_sha256': graph_hashes,
                    'config_sha256': config_sha, 'official_py_sha256': official_hashes,
                    'source_unchanged_before_after': True, 'source_files_checked': len(tracked),
                    'document_snapshots': document_snapshots,
                    'original_local_paths_in_optional_snapshot': snapshot,
                    'builder_sha256': tracked[Path(__file__).resolve()], 'verifier_sha256': tracked[Path(v.__file__).resolve()],
                    'original_catalog_sha256': tracked[portfolio/'catalog.csv'],
                    'original_portfolio_manifest_sha256': tracked[portfolio/'manifest.json'],
                    'runtime_requirement': 'Python 3.12, standard library, CPU; no GPU required',
                    'scope': 'best-known portfolio from saved successful evaluations; not equal-budget algorithm, not global optimum',
                    'tests': 'project archived-fixture tests excluded; portable verifier is provided; no fresh official evaluations performed'}
        save(stage/'delivery_manifest.json', manifest)
        (stage/'MANIFEST.sha256').write_text(v.sha(stage/'delivery_manifest.json') + '\n', encoding='ascii')
        verified = v.verify_delivery(stage, allow_partial=allow_partial)
        for path, value in tracked.items(): v.require(v.sha(path) == value, 'source changed during final verification: ' + str(path))
        publish_new(stage, out); published = True
        return {**verified, 'output': str(out), 'source_files_checked_before_after': len(tracked)}
    finally:
        if not published and stage.exists(): shutil.rmtree(stage)


# PurePosixPath keeps the packaged JSON independent of the host path separator.
from pathlib import PurePosixPath as PurePath


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--portfolio', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-partial', action='store_true')
    parser.add_argument('--snapshot', action='store_true', help='Also retain original local provenance/records as optional tracking material')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build_delivery(args.portfolio, args.output, allow_partial=args.allow_partial, snapshot=args.snapshot), ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'build_failed', 'error': '{}: {}'.format(type(error).__name__, error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
