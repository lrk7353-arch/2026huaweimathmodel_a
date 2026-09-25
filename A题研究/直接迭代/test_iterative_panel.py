import unittest
from freeze_iterative_panel import select
from run_iterative_panel import extension_signal


class PanelTests(unittest.TestCase):
    def test_structural_selection_ignores_scores_and_excludes_development_cases(self):
        rows = [dict(case=f'case_{i:03d}',compute_ops=i,log_ops=i,
            log_components=i%3,largest_component_fraction=1/(i%4+1),
            log_tasks_n5=i%7,log_median_task_ops_n5=i%5) for i in range(1,101)]
        first = select(rows)
        second = select([dict(x,observed_makespan=1000-i) for i,x in enumerate(rows)])
        self.assertEqual([x['case'] for x in first],[x['case'] for x in second])
        self.assertEqual(len({x['case'] for x in first}),6)
        self.assertTrue({x['case'] for x in first}.isdisjoint({'case_016','case_062','case_063','case_100'}))
        self.assertEqual([x['size_group'] for x in first],[0,0,1,1,2,2])

    def test_extension_requires_multiple_capped_cells_and_late_refresh_gain(self):
        rows = [dict(case=f'case_{i:03d}',cores=2,method='iterative',status='success',stop_reason='call_budget') for i in (1,2)]
        trials = {(x['case'],2):[dict(accepted=False,generation=2)] for x in rows}
        self.assertFalse(extension_signal(rows,trials))
        trials['case_001',2].append(dict(accepted=True,generation=2))
        self.assertTrue(extension_signal(rows,trials))
        self.assertFalse(extension_signal(rows[:1],trials))

    def test_old_improvement_alone_does_not_trigger_more_budget(self):
        rows = [dict(case=f'case_{i:03d}',cores=2,method='iterative',status='success',stop_reason='call_budget') for i in (1,2)]
        trials = {(x['case'],2):[dict(accepted=True,generation=2),dict(accepted=False,generation=3),dict(accepted=False,generation=3)] for x in rows}
        self.assertFalse(extension_signal(rows,trials))


if __name__ == '__main__':
    unittest.main()
