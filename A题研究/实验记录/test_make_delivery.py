"""Synthetic fixture only: packaging/relocation/tamper tests, zero official calls."""
import copy
import csv
import gzip
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import make_delivery as m
import verify_delivery as v


def fixture(root):
    workspace = root / 'original_machine'
    portfolio = workspace / 'A题研究/当前最佳方案'
    for folder, names in m.MINIMUM_RUNTIME.items():
        for name in names:
            path = workspace / folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# synthetic runtime fixture only\n', encoding='utf-8')
    config = workspace / v.DATA / 'config.txt'; config.parent.mkdir(parents=True)
    config.write_text('\n\n'.join('[' + k + ']\n' + '\n'.join(n + ' ' + str(x) for n, x in values.items()) for k, values in v.FIXED_CONFIG.items()), encoding='utf-8')
    graph = {'ops': [{'id': 0, 'op': 'COPY_IN', 'cycles': 0, 'pipe': 'PIPE_MTE2'}, {'id': 2, 'op': 'A', 'cycles': 100, 'pipe': 'PIPE_M'}],
             'tensors': [{'id': 1, 'size': 4, 'pos': 'L1'}], 'edges': [{'source': 0, 'target': 1}, {'source': 1, 'target': 2}]}
    for case in v.GRAPH_NAMES: m.save(workspace/v.DATA/(case+'.json'), graph)
    official = {p.name: v.sha(p) for p in (workspace/v.CODE).glob('*.py')}
    rows = []
    plan = {'node_to_subgraph': {'2': 0}, 'core_schedules': [[0], []]}
    for problem in (2, 3):
        case, cores = 'case_001', 2
        selected = portfolio / ('p{}/n2/case_001_multicore_res.json'.format(problem))
        m.save(selected, plan)
        attempt = workspace / ('old_runs/p{}'.format(problem)); attempt.mkdir(parents=True)
        evalplan = attempt/'plan.json'; evalplan.write_bytes(v.json_bytes(plan))
        raw = {'scene': 'B', 'makespan': 100 + problem, 'num_cores': cores,
               'input_graph': case+'.json', 'input_plan': 'plan.json',
               'capacity_bytes': v.FIXED_CONFIG['capacity'], 'bandwidth_bytes_per_cycle': 60,
               'cross_core_copy_delay_cycles': 500,
               'data_movement_bytes': {'original_graph_copy_bytes': 8, 'scheduled_copy_bytes': 8, 'added_copy_bytes': 0, 'partition_added_copy_bytes': 0, 'spill_added_copy_bytes': 0}}
        raw['per_core_timeline'] = [
            {'core_id': 0, 'tasks': [{'task_id': 0, 'subgraph_ids': [0]}], 'ops': [
                {'op_id': 0, 'op': 'COPY_IN', 'pipe': 'PIPE_MTE2', 'task_id': 0, 'subgraph_id': 0, 'start': 0, 'end': problem, 'duration': problem},
                {'op_id': 2, 'op': 'A', 'pipe': 'PIPE_M', 'task_id': 0, 'subgraph_id': 0, 'start': problem, 'end': 100+problem, 'duration': 100}]},
            {'core_id': 1, 'tasks': [], 'ops': []}]
        if problem == 3:
            raw.update(problem=3, cache_mode='read_only', cache_capacity_bytes=1048576, cache_bandwidth_bytes_per_cycle=250,
                       cache_stats={'copy_in_hits': 0, 'copy_in_misses': 1, 'hit_bytes': 0, 'miss_bytes': 4, 'hits': 0, 'accesses': 1, 'hit_rate': 0.0})
        result = attempt/'official_result.json.gz'; result.write_bytes(gzip.compress(v.json_bytes(raw), mtime=0))
        metrics = {k: raw[k] for k in v.RAW_METRICS if k in raw}; metrics.update(active_cores=1, peak_memory_bytes=12000)
        record = {'status': 'success', 'problem': problem, 'metrics': metrics, 'result_path': str(result),
                  'result_sha256': v.sha(result), 'plan_path': str(evalplan), 'record_path': str(attempt/'record.json'),
                  'graph_path': str(workspace/v.DATA/(case+'.json')), 'config_path': str(config), 'official_code': str(workspace/v.CODE),
                  'peak_memory_bytes': 12000, 'returncode': 0,
                  'hashes': {'graph_sha256': v.sha(workspace/v.DATA/(case+'.json')), 'config_sha256': v.sha(config),
                             'plan_sha256': v.sha(evalplan), 'problem': problem, 'official_py_sha256': official,
                             'wrapper_sha256': v.sha(workspace/v.SOLVER/'evaluator.py'), 'worker_sha256': v.sha(workspace/v.SOLVER/'eval_worker.py')}}
        m.save(record['record_path'], record)
        row = {'case': case, 'problem': problem, 'num_cores': cores, 'makespan': metrics['makespan'], 'added_copy_bytes': 0,
               'active_cores': 1, 'origin': str(attempt/'record.json'), 'plan_path': str(selected), 'plan_sha256': v.sha(selected), 'official_result_path': str(result)}
        rows.append(row); m.save(selected.with_suffix('.provenance.json'), {**row, 'evaluation_record': record})
    with (portfolio/'catalog.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    m.save(portfolio/'manifest.json', {'selected_count': len(rows), 'source_hashes': official, 'config_sha256': v.sha(config), 'rejected': []})
    return workspace, portfolio


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.workspace, self.portfolio = fixture(self.root)
        self.output = self.root/'delivery'

    def build(self, **kwargs):
        return m.build_delivery(self.portfolio, self.output, workspace=self.workspace, **kwargs)

    def rewrite_manifest_inventory(self, relative):
        manifest = v.read(self.output/'delivery_manifest.json')
        path = self.output/relative
        manifest['inventory'][relative] = {'sha256': v.sha(path), 'bytes': path.stat().st_size}
        m.save(self.output/'delivery_manifest.json', manifest)
        (self.output/'MANIFEST.sha256').write_text(v.sha(self.output/'delivery_manifest.json')+'\n')

    def test_missing_coverage_rejected_before_output_created(self):
        with self.assertRaisesRegex(ValueError, 'incomplete portfolio'): self.build()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob('.delivery.building-*')))

    def test_partial_explicit_and_relocated_without_original_machine(self):
        result = self.build(allow_partial=True)
        self.assertEqual(result['selected_count'], 2)
        self.assertEqual(result['missing_count'], 1498)
        with self.assertRaisesRegex(ValueError, 'partial'): v.verify_delivery(self.output)
        moved = self.root/'moved'/'bundle'; moved.parent.mkdir(); self.output.rename(moved)
        shutil.rmtree(self.workspace)
        answer = subprocess.run([sys.executable, '-B', str(moved/'verify_delivery.py'), '--allow-partial'], cwd=self.root,
                                text=True, capture_output=True)
        self.assertEqual(answer.returncode, 0, answer.stderr)
        self.assertEqual(json.loads(answer.stdout)['status'], 'verified_partial')
        for name in ('delivery_index.json', 'delivery_manifest.json'):
            self.assertNotIn(str(self.workspace), (moved/name).read_text())
        for path in moved.glob('A题研究/当前最佳方案/p*/n*/*.provenance.json'):
            self.assertNotIn(str(self.workspace), path.read_text())

    def test_snapshot_is_optional_history_not_execution_dependency(self):
        self.build(allow_partial=True, snapshot=True)
        self.assertTrue((self.output/'source_snapshot/original_catalog.csv').exists())
        shutil.rmtree(self.workspace)
        self.assertEqual(v.verify_delivery(self.output, allow_partial=True)['selected_count'], 2)

    def test_duplicate_catalog_rejected(self):
        path = self.portfolio/'catalog.csv'
        text = path.read_text(encoding='utf-8-sig'); path.write_text(text+text.splitlines()[1]+'\n', encoding='utf-8-sig')
        with self.assertRaisesRegex(ValueError, 'duplicate'): self.build(allow_partial=True)
        self.assertFalse(self.output.exists())

    def test_missing_and_corrupt_inventory_file_rejected(self):
        self.build(allow_partial=True)
        path = self.output/v.DATA/'case_050.json'
        original = path.read_bytes(); path.write_bytes(original+b' ')
        with self.assertRaisesRegex(ValueError, 'inventory'): v.verify_delivery(self.output, allow_partial=True)
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'missing'): v.verify_delivery(self.output, allow_partial=True)

    def test_metric_tampering_detected_even_if_inventory_rehashed(self):
        self.build(allow_partial=True)
        item = v.read(self.output/'delivery_index.json')[0]
        relative = item['provenance_path']; prov = v.read(self.output/relative)
        prov['evaluation_record']['metrics']['makespan'] += 1
        m.save(self.output/relative, prov); self.rewrite_manifest_inventory(relative)
        with self.assertRaisesRegex(ValueError, 'metric mismatch'): v.verify_delivery(self.output, allow_partial=True)

    def test_wrong_scene_detected_from_raw_even_if_inventory_rehashed(self):
        self.build(allow_partial=True)
        item = v.read(self.output/'delivery_index.json')[0]
        relative = item['provenance_path']; prov = v.read(self.output/relative)
        prov['evaluation_record']['problem'] = 1
        m.save(self.output/relative, prov); self.rewrite_manifest_inventory(relative)
        with self.assertRaisesRegex(ValueError, 'problem mismatch'): v.verify_delivery(self.output, allow_partial=True)

    def test_raw_plan_mismatch_detected_even_after_rehashing(self):
        self.build(allow_partial=True)
        item = v.read(self.output/'delivery_index.json')[0]
        relative = item['provenance_path']; prov = v.read(self.output/relative)
        raw_name = prov['evaluation_record']['result_path']; raw_path = self.output/raw_name
        raw = json.loads(gzip.decompress(raw_path.read_bytes()))
        raw['per_core_timeline'][0]['ops'][1]['subgraph_id'] = 999
        raw_path.write_bytes(gzip.compress(v.json_bytes(raw), mtime=0))
        prov['evaluation_record']['result_sha256'] = v.sha(raw_path)
        m.save(self.output/relative, prov)
        self.rewrite_manifest_inventory(raw_name); self.rewrite_manifest_inventory(relative)
        with self.assertRaisesRegex(ValueError, 'raw timeline'): v.verify_delivery(self.output, allow_partial=True)

    def test_dynamic_hash_dependency_missing_even_if_inventory_adjusted(self):
        self.build(allow_partial=True)
        name = 'A题研究/advanced_solver/supervisor.py'
        (self.output/name).unlink()
        manifest = v.read(self.output/'delivery_manifest.json')
        del manifest['inventory'][name]; manifest['runtime_code_paths'].remove(name)
        m.save(self.output/'delivery_manifest.json', manifest)
        (self.output/'MANIFEST.sha256').write_text(v.sha(self.output/'delivery_manifest.json')+'\n')
        with self.assertRaisesRegex(ValueError, 'runtime/hash-manifest'): v.verify_delivery(self.output, allow_partial=True)

    def test_plan_file_sha_and_evaluated_object_sha_are_distinct(self):
        self.build(allow_partial=True)
        item = v.read(self.output/'delivery_index.json')[0]
        self.assertNotEqual(item['plan_file_sha256'], item['plan_sha256'])
        self.assertEqual(v.verify_delivery(self.output, allow_partial=True)['selected_count'], 2)

    def test_source_plan_hash_error_and_missing_raw_rejected(self):
        path = self.portfolio/'p2/n2/case_001_multicore_res.json'
        path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError, 'SHA mismatch'): self.build(allow_partial=True)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob('.delivery.building-*')))

    def test_missing_original_gzip_is_rejected(self):
        (self.workspace/'old_runs/p2/official_result.json.gz').unlink()
        with self.assertRaisesRegex(ValueError, 'source file missing'): self.build(allow_partial=True)
        self.assertFalse(self.output.exists())

    def test_source_mutation_during_copy_refuses_publication(self):
        original = shutil.copyfile
        target = (self.workspace/v.SOLVER/'baselines.py').resolve()
        def copy_then_mutate(source, destination):
            result = original(source, destination)
            if Path(source) == target:
                target.write_text('# changed after copy\n', encoding='utf-8')
            return result
        with patch.object(m.shutil, 'copyfile', side_effect=copy_then_mutate):
            with self.assertRaisesRegex(ValueError, 'source changed'): self.build(allow_partial=True)
        self.assertFalse(self.output.exists())

    def test_forged_full_coverage_flag_is_rejected(self):
        self.build(allow_partial=True)
        manifest = v.read(self.output/'delivery_manifest.json'); manifest['partial'] = False
        m.save(self.output/'delivery_manifest.json', manifest)
        (self.output/'MANIFEST.sha256').write_text(v.sha(self.output/'delivery_manifest.json')+'\n')
        with self.assertRaisesRegex(ValueError, 'coverage'): v.verify_delivery(self.output)

    def test_output_collision_and_official_path_rejected(self):
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, 'new output'): self.build(allow_partial=True)
        with self.assertRaisesRegex(ValueError, 'protected'):
            m.build_delivery(self.portfolio, self.workspace/v.DATA/'new_output', workspace=self.workspace, allow_partial=True)

    def test_racing_empty_output_is_never_replaced(self):
        original = m.publish_new
        def race(stage, output):
            output.mkdir()
            return original(stage, output)
        with patch.object(m, 'publish_new', side_effect=race):
            with self.assertRaises(FileExistsError): self.build(allow_partial=True)
        self.assertTrue(self.output.is_dir())
        self.assertEqual(list(self.output.iterdir()), [])

    def test_dangling_output_symlink_is_rejected(self):
        self.output.symlink_to(self.root/'missing_target', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'new output'): self.build(allow_partial=True)
        self.assertTrue(self.output.is_symlink())

    def test_portable_relative_path_escape_and_extra_field_rejected(self):
        self.build(allow_partial=True)
        item = v.read(self.output/'delivery_index.json')[0]
        relative = item['provenance_path']; prov = v.read(self.output/relative)
        prov['evaluation_record']['result_path'] = '../../outside.gz'
        m.save(self.output/relative, prov); self.rewrite_manifest_inventory(relative)
        with self.assertRaisesRegex(ValueError, 'escaping'): v.verify_delivery(self.output, allow_partial=True)

    def test_invalid_plan_coverage_rejected_independently(self):
        plan = {'node_to_subgraph': {'999': 0}, 'core_schedules': [[0], []]}
        view = v.graph_view(v.read(self.workspace/v.DATA/'case_001.json'))
        with self.assertRaisesRegex(ValueError, 'exactly cover'): v.verify_plan(plan, view, 2)
        plan = {'node_to_subgraph': {'2': 0}, 'core_schedules': [[0], []], 'sleep': 3}
        with self.assertRaisesRegex(ValueError, 'exactly two'): v.verify_plan(plan, view, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
