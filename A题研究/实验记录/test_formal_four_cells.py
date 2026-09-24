"""No evaluator subprocess: fake-controller tests plus read-only archived evidence."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).with_name('formal_four_cells.py')
SPEC = importlib.util.spec_from_file_location('formal_four_cells', PATH)
f = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(f)


class FourCellMath(unittest.TestCase):
    def test_archived_10_graphs_exact_metrics_and_negative_policy(self):
        launch = f.build_launch(f.FORMAL, 1)
        data = f.read_json(f.HERE / 'P3四格_v2/summary.json')
        self.assertEqual(len(data['cases']), 10)
        found = {}
        for row in data['cases']:
            cells = row['cells']
            for name, record in cells.items():
                f.verify_record(record, problem=int(name[1]), cores=5,
                                graph_sha=record['hashes']['graph_sha256'], config_sha=launch['config_sha256'],
                                source_hashes=launch['source_hashes'], plan=f.read_json(record['plan_path']))
            r = f.ratios(cells)
            self.assertAlmostEqual(r['hardware_on_pi2'] * r['policy_in_P3'], r['total'])
            found[row['case']] = r
        self.assertLess(found['case_071']['policy_in_P3'], 1)
        self.assertEqual(found['case_093']['hardware_on_pi2'], 1)
        self.assertGreater(found['case_093']['policy_in_P3'], 1)

    def test_full_100_only_aggregate(self):
        rows = [{'case': f'case_{i:03d}', 'num_cores': 2, 'four_cell_complete': True,
                 'ratios': {'hardware_on_pi2': 2, 'policy_in_P3': .9, 'total': 1.8},
                 'singlecore_makespan': 20, 'metrics': {k: {'makespan': 10} for k in ('t2_pi2', 't3_pi2', 't2_pi3', 't3_pi3')}} for i in range(1, 101)]
        self.assertIsNone(f.aggregate(rows[:99])['2']['ratio_summary'])
        rows[3]['four_cell_complete'] = False
        self.assertIsNone(f.aggregate(rows)['2']['ratio_summary'])
        rows[3]['four_cell_complete'] = True
        self.assertEqual(f.aggregate(rows)['2']['official_mean_singlecore_over_T']['t2_pi2'], 2)
        self.assertIsNone(f.aggregate(rows)['3']['ratio_summary'])

    def test_timeout_compute_only_boundary(self):
        self.assertEqual(f.timeout_for_ops(10000), 60)
        self.assertEqual(f.timeout_for_ops(10001), 180)

    def test_pending_main_cannot_freeze(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            launch = {'quick_wait_hashes': {}, 'mains': {'2': str(out/'p2'), '3': str(out/'p3')}}
            with self.assertRaises(RuntimeError):
                f.wait_for_main(launch, out, wait=False, max_wait_seconds=1, interval=1)
            self.assertFalse(f.read_json(out/'dependency_progress.json')['official_cross_calls_started'])

    def test_output_cannot_cover_formal_or_existing_experiment(self):
        for out in (f.FORMAL, f.FORMAL/'full_p2_seed17', f.RESEARCH, f.HERE/'P3四格_v2'):
            with self.assertRaises(ValueError):
                f.validate_output_path(out.resolve(), f.FORMAL.resolve(), resume=False)
        f.validate_output_path((f.HERE/'never_created_test_output').resolve(), f.FORMAL.resolve(), resume=False)


class FakeCampaign(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.calls = []
        self.next_status = 'success'
        plan = {'node_to_subgraph': {'1': 0}, 'core_schedules': [[0], []]}
        f.atomic_json(self.out/'plan.json', plan)
        (self.out/'graph.json').write_text('{}')
        (self.out/'config.txt').write_text('fixed')
        self.launch = {'formal_dir': str(self.out/'eval'), 'quick_wait_hashes': {}, 'source_hashes': {},
                       'config': str(self.out/'config.txt'), 'config_sha256': f.digest(self.out/'config.txt'), 'workers': 1}
        self.item = {'id': 'case_001_n2', 'case': 'case_001', 'num_cores': 2,
                     'graph_path': str(self.out/'graph.json'), 'graph_sha256': f.digest(self.out/'graph.json'),
                     'timeout': 60, 'selection_errors': {}, 'singlecore': None, 'singlecore_error': None}
        for name in ('pi2', 'pi3'):
            self.item[name] = {'plan_path': str(self.out/'plan.json'), 'plan_sha256': f.object_digest(plan),
                               'plan_file_sha256': f.digest(self.out/'plan.json')}
        f.atomic_json(self.out/'selection.json', self.item)
        self.manifest = {'selection': [{'id': self.item['id'], 'path': str(self.out/'selection.json'), 'sha256': f.digest(self.out/'selection.json')}], 'fixed_hashes': {}}
        f.atomic_json(self.out/'selection_manifest.json', self.manifest)
        self.verify = patch.object(f, 'verify_record', lambda *a, **k: {})
        self.verify.start(); self.addCleanup(self.verify.stop)

    def evaluate(self, graph, plan, problem, run_dir, **kwargs):
        self.calls.append((problem, copy.deepcopy(plan)))
        attempt = self.out / ('fake_{}'.format(len(self.calls)))
        attempt.mkdir()
        record = {'problem': problem, 'status': self.next_status, 'cache_hit': True, 'metrics': {'makespan': 50, 'num_cores': 2},
                  'record_path': str(attempt/'record.json'), 'hashes': {'plan_sha256': f.object_digest(plan)}}
        f.atomic_json(attempt/'record.json', record)
        return record

    def campaign(self, **kwargs):
        return f.Campaign(self.launch, self.manifest, self.out, evaluator=self.evaluate, **kwargs)

    def test_cache_reuse_still_logical_and_resume_no_calls(self):
        c = self.campaign()
        c.cell(self.item, 't3_pi2')
        c.cell(self.item, 't2_pi3')
        self.assertEqual(c.count, 2)
        self.assertEqual(len(self.calls), 2)
        again = self.campaign()
        again.cell(self.item, 't3_pi2')
        again.cell(self.item, 't2_pi3')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(f.read_json(self.out/'progress.json')['cache_hits'], 2)

    def test_failure_kept_until_explicit_retry(self):
        self.next_status = 'timeout'
        c = self.campaign()
        c.cell(self.item, 't3_pi2')
        self.next_status = 'success'
        self.campaign().cell(self.item, 't3_pi2')
        self.assertEqual(len(self.calls), 1)
        retry = self.campaign(new_attempt=['case_001_n2/t3_pi2'])
        retry.cell(self.item, 't3_pi2')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([x['record']['status'] for x in retry.entries['case_001_n2/t3_pi2']], ['timeout', 'success'])

    def test_reserved_crash_consumes_budget_without_auto_replay(self):
        c = self.campaign()
        c.cell(self.item, 't3_pi2')
        row = c.entries['case_001_n2/t3_pi2'][0]
        row['phase'] = 'reserved'; row.pop('record'); row.pop('record_file_sha256')
        f.atomic_json(row['journal_path'], row)
        again = self.campaign()
        self.assertEqual(again.cell(self.item, 't3_pi2')['status'], 'interrupted')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(again.count, 1)

    def test_bad_identity_and_record_sha_rejected(self):
        c = self.campaign(); c.cell(self.item, 't3_pi2')
        row = c.entries['case_001_n2/t3_pi2'][0]
        row['cell_id'] = 'case_002_n2/t3_pi2'
        f.atomic_json(row['journal_path'], row)
        with self.assertRaises(ValueError): self.campaign()
        row['cell_id'] = 'case_001_n2/t3_pi2'; row['record_file_sha256'] = 'tampered'
        f.atomic_json(row['journal_path'], row)
        with self.assertRaises(ValueError): self.campaign()

    def test_record_validation_failure_never_turns_to_success_on_resume(self):
        with patch.object(f, 'verify_record', side_effect=ValueError('fake mismatch')):
            c = self.campaign()
            self.assertNotEqual(c.cell(self.item, 't3_pi2')['status'], 'success')
        again = self.campaign()
        self.assertEqual(again.cell(self.item, 't3_pi2')['status'], 'controller_error')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(f.read_json(self.out/'progress.json')['status_counts'], {'controller_error': 1})

    def test_cap_not_expanded_by_explicit_retry(self):
        self.next_status = 'timeout'
        c = self.campaign(); c.cell(self.item, 't3_pi2')
        retry = self.campaign(new_attempt=['case_001_n2/t3_pi2'])
        retry.count = f.CAP  # Exercise refusal without 800 fake evaluator calls.
        self.assertEqual(retry.cell(self.item, 't3_pi2')['status'], 'logical_budget_exhausted')
        self.assertEqual(len(self.calls), 1)

    def test_wrapper_failure_without_persisted_record_is_retained(self):
        def broken(*args, **kwargs):
            self.calls.append(('broken', None))
            return {'status': 'runtime_error', 'cache_hit': False, 'metrics': {}, 'record_path': None, 'error': 'pre-worker failure'}
        c = f.Campaign(self.launch, self.manifest, self.out, evaluator=broken)
        self.assertNotEqual(c.cell(self.item, 't3_pi2')['status'], 'success')
        self.assertEqual(c.entries['case_001_n2/t3_pi2'][0]['record']['error'], 'pre-worker failure')
        again = self.campaign()
        self.assertEqual(again.cell(self.item, 't3_pi2')['status'], 'controller_error')
        self.assertEqual(len(self.calls), 1)
        progress = f.read_json(self.out/'progress.json')
        self.assertEqual(progress['confirmed_new_worker_calls'], 0)
        self.assertEqual(progress['uncached_recorded_calls'], 1)

    def test_end_to_end_fake_pair_outputs_all_cells_without_partial_mean(self):
        for label, problem in (('pi2', 2), ('pi3', 3)):
            self.item[label]['record'] = {'problem': problem, 'status': 'success', 'cache_hit': False,
                    'metrics': {'makespan': 100, 'num_cores': 2},
                    'hashes': {'plan_sha256': self.item[label]['plan_sha256']}}
        f.atomic_json(self.out/'selection.json', self.item)
        self.manifest['selection'][0]['sha256'] = f.digest(self.out/'selection.json')
        f.atomic_json(self.out/'selection_manifest.json', self.manifest)
        self.launch['scope'] = {'cases': ['case_001'], 'num_cores': [2], 'N1_included': False}
        result = self.campaign().run()
        self.assertEqual(result['logical_cross_calls'], 2)
        self.assertEqual(result['four_cell_complete_count'], 1)
        self.assertFalse(result['all_four_cells_complete'])
        self.assertIsNone(result['by_num_cores']['2']['ratio_summary'])
        pair = f.read_json(self.out/'pairs/case_001_n2/four_cells.json')
        self.assertEqual(len(pair['cells']), 4)
        self.assertTrue((self.out/'four_cells.csv').is_file())


if __name__ == '__main__':
    unittest.main(verbosity=2)
