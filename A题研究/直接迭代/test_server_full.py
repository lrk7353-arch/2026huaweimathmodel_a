import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from common_run import atomic_json,read_json
from run_server_full import run_jobs,apply_replay_recovery,recover_paid_incumbent


class ResumeTests(unittest.TestCase):
    def test_pause_marker_stops_dispatch_after_active_work_finishes(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            jobs=[dict(id=str(i),kind='cold',case='case_001',problem=1,cores=1) for i in range(3)]
            def run(job,path):
                (root/'pause.request').write_text('pause')
                return dict(job=job,status='success',calls=1)
            with patch('run_server_full.supervise',side_effect=run) as worker:
                results=run_jobs(jobs,root,1,time.time()+1000)
            self.assertEqual(len(results),1)
            self.assertEqual(worker.call_count,1)
            self.assertTrue(read_json(root/'progress.json')['paused'])
            self.assertFalse(read_json(root/'progress.json')['complete'])

    def test_optimizer_crash_retains_only_paid_local_verified_incumbent(self):
        import hashlib
        from common_run import DATA
        from test_p1_task_refine import graph
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);attempt=root/'search/evaluations/attempts/paid';attempt.mkdir(parents=True)
            plan=dict(node_to_subgraph={'0':0},core_schedules=[[0]])
            atomic_json(attempt/'plan.json',plan)
            (attempt/'official_result.json.gz').write_bytes(b'official result fixture')
            hashes=dict(plan_sha256=hashlib.sha256((attempt/'plan.json').read_bytes()).hexdigest())
            record=dict(status='success',problem=1,graph_path=str(DATA/'case_001.json'),
                plan_path=str(attempt/'plan.json'),record_path=str(attempt/'record.json'),
                result_path=str(attempt/'official_result.json.gz'),hashes=hashes,
                result_sha256=hashlib.sha256((attempt/'official_result.json.gz').read_bytes()).hexdigest(),
                metrics=dict(num_cores=1,makespan=10,data_movement_bytes=dict(added_copy_bytes=0)))
            atomic_json(attempt/'record.json',record)
            atomic_json(attempt/'request.json',dict(problem=1,hashes=hashes,graph_path=record['graph_path'],plan_path=record['plan_path']))
            (root/'search/evaluations/attempts/timed_out').mkdir()
            job=dict(kind='cold',case='case_001',problem=1,cores=1)
            failed=dict(status='interrupted_or_hard_timeout',calls=2,returncode=-9)
            with patch('run_server_full.GraphIR.from_path',return_value=graph(1,[])):
                recovered=recover_paid_incumbent(job,root,failed)
                self.assertEqual(recovered['status'],'success')
                self.assertEqual(recovered['calls'],2)
                self.assertEqual(recovered['optimizer_termination'],failed['status'])
                self.assertEqual(failed['status'],'interrupted_or_hard_timeout')
                (attempt/'official_result.json.gz').write_bytes(b'corrupted')
                self.assertIs(recover_paid_incumbent(job,root,failed),failed)

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
