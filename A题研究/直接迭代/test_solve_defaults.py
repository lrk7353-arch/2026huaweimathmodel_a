"""Exercise the CLI dispatch so new cold defaults do not alter P1/warm modes."""
import contextlib
import io
import tempfile
import unittest
import importlib.util
from pathlib import Path
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location('direct_solver_entry', Path(__file__).with_name('solve.py'))
solve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(solve)


class EntryTests(unittest.TestCase):
    def invoke(self, args, target):
        with tempfile.TemporaryDirectory() as folder:
            result=dict(best_record=dict(metrics=dict(makespan=10, data_movement_bytes=dict(added_copy_bytes=0))),
                        logical_calls=1,new_calls=1,elapsed_seconds=.1)
            with patch('sys.argv',['solve.py','--case','1','--out',str(Path(folder)/'out')]+args), \
                 patch(target,return_value=result) as run, \
                 patch.object(solve,'known',return_value={('case_001',3,5):result['best_record']}), \
                 contextlib.redirect_stdout(io.StringIO()):
                solve.main()
                return run.call_args

    def test_p23_cold_uses_validated_method_and_budget_defaults(self):
        for p in (2,3):
            call=self.invoke(['--problem',str(p),'--from-scratch'],'p23_pipeline.run')
            self.assertEqual(call.args[3],'trace_routed')
            self.assertEqual(call.args[5:7],(12,120))

    def test_explicit_legacy_pipeline_and_budget_stay_explicit(self):
        call=self.invoke(['--problem','2','--from-scratch','--p23-method','staged',
                          '--budget','5','--seconds','19'],'p23_pipeline.run')
        self.assertEqual(call.args[3],'staged')
        self.assertEqual(call.args[5:7],(5,19))

    def test_p1_and_p3_warm_defaults_are_preserved(self):
        call=self.invoke(['--problem','1','--from-scratch'],'p1_adaptive.run')
        self.assertEqual(call.args[2],'adaptive')
        self.assertEqual(call.args[4:6],(8,180))
        call=self.invoke(['--problem','3'],'run_p3_refine.run')
        self.assertEqual(call.args[4:6],(8,180))

    def test_explicit_integrated_entry_preserves_requested_budget(self):
        call=self.invoke(['--problem','1','--from-scratch','--p1-method','integrated',
                          '--budget','12','--seconds','90','--evaluation-timeout','25'],'p1_adaptive.run')
        self.assertEqual(call.args[2],'integrated');self.assertEqual(call.args[4:6],(12,90))
        call=self.invoke(['--problem','3','--from-scratch','--p23-method','integrated',
                          '--budget','12','--fresh-evaluations'],'p23_pipeline.run')
        self.assertEqual(call.args[3],'integrated');self.assertEqual(call.args[5],12)
        self.assertEqual(call.kwargs['evaluation_dir'].name,'evaluations')

    def test_budget_beam_is_explicit_and_preserves_budget(self):
        call=self.invoke(['--problem','2','--from-scratch','--p23-method','budget_beam',
                          '--budget','12','--seconds','90'],'p23_pipeline.run')
        self.assertEqual(call.args[3],'budget_beam');self.assertEqual(call.args[5:7],(12,90))


if __name__=='__main__':unittest.main()
