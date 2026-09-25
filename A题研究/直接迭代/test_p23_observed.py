"""Timeline identity, interval semantics and proxy-choice contracts."""
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import p23_observed as observed


class ObservedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = {'node_to_subgraph': {'10': 0, '20': 0, '30': 1}, 'core_schedules': [[0], [1]]}
        self.raw = dict(problem=3, scene='B', num_cores=2, makespan=100, per_core_timeline=[
            dict(core_id=0, ops=[dict(op_id=10, pipe='PIPE_M', start=0, end=80),
                                 dict(op_id=20, pipe='PIPE_V', start=30, end=90),
                                 dict(op_id=999, pipe='PIPE_M', start=80, end=100)]),
            dict(core_id=1, ops=[dict(op_id=30, pipe='PIPE_V', start=5, end=25)])])
        self.record = dict(problem=3, plan_path=str(self.root/'plan.json'),
                           result_path=str(self.root/'raw.json.gz'), record_path='synthetic',
                           metrics=dict(makespan=100, data_movement_bytes=dict(spill_added_copy_bytes=0, added_copy_bytes=0)))
        self.ir = SimpleNamespace(compute_ids=[10, 20, 30])

    def inspect(self):
        (self.root/'plan.json').write_text(json.dumps(self.plan))
        with gzip.open(self.root/'raw.json.gz', 'wt') as f:
            json.dump(self.raw, f)
        with patch.object(observed, 'validate_plan'):
            return observed.timeline_features(self.ir, self.record, 2)

    def test_only_original_compute_counts_and_overlap_is_interval_intersection(self):
        result = self.inspect()
        self.assertEqual(result['largest_core_pipe_busy'], 80)
        self.assertAlmostEqual(result['fixed_assignment_headroom'], .20)
        self.assertEqual(result['per_core'][0]['mv_overlap'], 50)
        self.assertAlmostEqual(result['per_core'][0]['non_mv_fraction'], .10)

    def test_incomplete_or_misassigned_trace_is_rejected(self):
        self.raw['per_core_timeline'][1]['ops'][0]['op_id'] = 400
        with self.assertRaisesRegex(ValueError, 'exact compute assignment'):
            self.inspect()
        self.raw['per_core_timeline'][1]['ops'][0]['op_id'] = 10
        with self.assertRaisesRegex(ValueError, 'Duplicate original'):
            self.inspect()

    def test_original_p2_scene_schema_and_explicit_wrong_problem(self):
        self.record['problem'] = 2
        del self.raw['problem']
        self.assertAlmostEqual(self.inspect()['fixed_assignment_headroom'], .2)
        self.raw['problem'] = 3
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            self.inspect()

    def test_route_uses_current_headroom_and_avoids_trace_for_large_components(self):
        with patch.object(observed, 'timeline_features', return_value={'fixed_assignment_headroom': .02}) as inspect:
            route = observed.observed_route(self.ir, self.record, 2, {'route': 'component_wcc'})
            self.assertEqual((route['route'], route['probe']), ('staged', True))
            inspect.return_value = {'fixed_assignment_headroom': .25}
            route = observed.observed_route(self.ir, self.record, 2, {'route': 'component_wcc'})
            self.assertEqual((route['route'], route['probe']), ('component_wcc', False))
            inspect.reset_mock()
            route = observed.observed_route(self.ir, self.record, 2, {'route': 'staged'})
            inspect.assert_not_called()
            self.assertFalse(route['probe'])

    def test_probe_avoids_seen_plans_and_prioritizes_memory_risk_before_compute_proxy(self):
        def candidate(name, cycles, memory):
            return dict(name=name, plan=name, metadata=dict(strategy='window_interleave',
                per_core_proxy=[dict(compute_only_predicted_end=cycles,
                                     compute_touch_live_bytes_proxy={'UB': memory})]))
        values = [candidate('seen', 1, 5), candidate('overflow', 20, 120),
                  candidate('good', 50, 80), candidate('slower', 70, 10)]
        result, details = observed.select_wcc_probe(values, {'seen'}, str, {'UB': 100, 'L1': 100})
        self.assertEqual(result['name'], 'good')
        self.assertEqual(details['memory_proxy_ratio'], .8)


if __name__ == '__main__':
    unittest.main()
