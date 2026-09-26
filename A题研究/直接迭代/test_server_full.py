import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from common_run import atomic_json,read_json
from run_server_full import run_jobs,apply_replay_recovery


class ResumeTests(unittest.TestCase):
    def test_recovery_rejects_changed_plan_and_keeps_original_failure(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            plan=dict(node_to_subgraph={'1':0},core_schedules=[[0]])
            atomic_json(root/'old_plan.json',plan);atomic_json(root/'new_plan.json',plan)
            hashes=dict(graph_sha256='g',config_sha256='c',official_py_sha256={'official.py':'same'})
            before=dict(status='timeout',plan_path=str(root/'old_plan.json'),hashes=hashes)
            after=dict(status='success',problem=1,metrics=dict(num_cores=1,makespan=10,
                       data_movement_bytes=dict(added_copy_bytes=0)),
                       plan_path=str(root/'new_plan.json'),hashes=hashes)
            atomic_json(root/'new_record.json',after)
            atomic_json(root/'replay_reconciliation.json',dict(records={'x':str(root/'new_record.json')}))
            original=dict(job=dict(id='x',problem=1,cores=1,expected_score=[10,0]),
                          status='mismatch_or_failure',best_record=before,calls=1)
            fixed=apply_replay_recovery([original],root)
            self.assertEqual(fixed[0]['status'],'success');self.assertEqual(fixed[0]['calls'],2)
            self.assertEqual(original['status'],'mismatch_or_failure')
            atomic_json(root/'new_plan.json',dict(node_to_subgraph={'1':5},core_schedules=[[5]]))
            with self.assertRaises(ValueError):apply_replay_recovery([original],root)

    def test_completion_order_does_not_invalidate_resume(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            jobs=[dict(id=n,kind='verify',case='case_001',problem=1,cores=1) for n in ('a','b')]
            atomic_json(root/'jobs.json',list(reversed(jobs)))
            for job in jobs:
                atomic_json(root/'jobs'/job['id']/'result.json',dict(job=job,status='success',calls=1))
            with patch('run_server_full.supervise',side_effect=AssertionError('must not rerun')):
                self.assertEqual(len(run_jobs(jobs,root,1,time.time()+1000)),2)

    def test_completed_job_is_not_paid_twice(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); job={'id':'done'}
            atomic_json(root/'jobs/done/result.json',dict(job=job,status='success',calls=1))
            # Minimal scheduling fixture must include reporting dimensions.
            job.update(kind='replay',case='case_001',problem=1,cores=1)
            atomic_json(root/'jobs/done/result.json',dict(job=job,status='success',calls=1))
            with patch('run_server_full.supervise',side_effect=AssertionError('must not rerun')):
                results=run_jobs([job],root,1,time.time()+1000)
            self.assertEqual(len(results),1)
            self.assertTrue(read_json(root/'progress.json')['complete'])
            changed=dict(job,cores=2)
            with self.assertRaises(ValueError):
                run_jobs([changed],root,1,time.time()+1000)

    def test_interrupted_charged_attempt_is_not_silently_restarted(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            job=dict(id='partial',kind='cold',case='case_001',problem=1,cores=1)
            atomic_json(root/'jobs/partial/search/attempt/request.json',{})
            with patch('run_server_full.supervise',side_effect=AssertionError('must not rerun')):
                result=run_jobs([job],root,1,time.time()+1000)
            self.assertEqual(result[0]['status'],'interrupted_previous_run')
            self.assertEqual(result[0]['calls'],1)
            self.assertFalse(read_json(root/'summary.json')['success'])

    def test_cutoff_leaves_work_pending_instead_of_fabricating_completion(self):
        with tempfile.TemporaryDirectory() as name:
            job=dict(id='queued',kind='cold',case='case_001',problem=1,cores=1)
            with patch('run_server_full.supervise',side_effect=AssertionError('deadline')):
                self.assertEqual(run_jobs([job],name,1,time.time()),[])
            self.assertFalse(read_json(Path(name)/'progress.json')['complete'])
            self.assertFalse((Path(name)/'summary.json').exists())


if __name__=='__main__':unittest.main()
