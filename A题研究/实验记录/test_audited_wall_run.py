import importlib.util
from pathlib import Path
import tempfile
import unittest

path = Path(__file__).with_name("audited_wall_run.py")
spec = importlib.util.spec_from_file_location("audited_wall_run", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
atomic_json = module.atomic_json


class InvocationAuditTests(unittest.TestCase):
    def test_uncommitted_completed_and_interrupted_calls_are_kept(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            atomic_json(root / "summary.json", {"evaluations": [{"record": {"attempt_id": "a"}}]})
            for name in ("a", "b", "c", "d"):
                atomic_json(root / "evaluations/attempts" / name / "plan.json", {})
            for name in ("a", "b"):
                atomic_json(root / "evaluations/attempts" / name / "record.json", {"status": "success", "cache_hit": False})
            atomic_json(root / "evaluations/attempts/c/request.json", {})
            atomic_json(root / "evaluations/attempts/c/progress.json", {"stage": "official"})
            audit = module.audit_attempts(root)
            self.assertEqual(4, audit["total_started_logical_calls"])
            self.assertEqual(1, audit["checkpoint_committed_calls"])
            self.assertEqual(3, audit["uncommitted_calls"])
            self.assertEqual(2, audit["interrupted_calls"])
            self.assertEqual("returned_record_not_committed", audit["attempts"][1]["phase"])

    def test_missing_committed_evidence_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            atomic_json(root / "summary.json", {"evaluations": [{"record": {"attempt_id": "missing"}}]})
            with self.assertRaises(ValueError):
                module.audit_attempts(root)


if __name__ == "__main__":
    unittest.main()
