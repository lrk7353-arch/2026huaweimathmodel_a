#!/usr/bin/env python3
"""Run the frozen hard-budget solver and account for interrupted invocations.

The frozen v2 search commits a trial only after evaluate returns. A hard kill
can interrupt its final call. This companion audit records every attempt, without
editing a checkpoint, claiming a missing result, or promoting an uncommitted plan.
"""
from pathlib import Path
import sys

RESEARCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RESEARCH))
from advanced_solver import supervisor
from advanced_solver.solve import parser
from solver.common import read_json, atomic_json, digest


def audit_attempts(run_dir):
    run_dir = Path(run_dir).resolve()
    summary = read_json(run_dir / "summary.json")
    committed = {r["record"]["attempt_id"] for r in summary.get("evaluations", [])}
    attempts = run_dir / "evaluations/attempts"
    rows = []
    for attempt in sorted(attempts.iterdir() if attempts.exists() else []):
        if not attempt.is_dir():
            continue
        record = read_json(attempt / "record.json") if (attempt / "record.json").exists() else None
        request = read_json(attempt / "request.json") if (attempt / "request.json").exists() else None
        progress = read_json(attempt / "progress.json") if (attempt / "progress.json").exists() else None
        if record:
            phase = "committed" if attempt.name in committed else "returned_record_not_committed"
            status = record["status"]
        else:
            phase = "interrupted_in_worker" if progress else "interrupted_after_request" if request else "interrupted_preparation"
            status = "interrupted"
        rows.append({"attempt_id": attempt.name, "attempt_dir": str(attempt), "phase": phase,
                     "status": status, "committed": attempt.name in committed,
                     "cache_hit": record.get("cache_hit") if record else None,
                     "worker_progress_observed": progress is not None,
                     "request_exists": request is not None,
                     "plan_sha256": digest(attempt / "plan.json") if (attempt / "plan.json").exists() else None})
    all_ids = {row["attempt_id"] for row in rows}
    if committed - all_ids:
        raise ValueError("committed attempts are missing; this audit requires a private cold evaluation directory")
    result = {"frozen_summary_path": str(run_dir / "summary.json"),
              "frozen_summary_sha256": digest(run_dir / "summary.json"),
              "scope": "all evaluator invocations including interrupted or uncommitted final call; no result promotion",
              "checkpoint_committed_calls": len(committed), "total_started_logical_calls": len(rows),
              "uncommitted_calls": sum(not r["committed"] for r in rows),
              "interrupted_calls": sum(r["status"] == "interrupted" for r in rows),
              "known_cache_hits": sum(r["cache_hit"] is True for r in rows),
              "observed_worker_invocations": sum(r["worker_progress_observed"] for r in rows),
              "attempts": rows, "audit_source_sha256": digest(__file__)}
    atomic_json(run_dir / "invocation_audit.json", result)
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = parser()
    p.add_argument("--wall-budget", type=float, required=True)
    args = p.parse_args(argv)
    if args.evaluation_dir:
        raise ValueError("wallclock experiment must use a private empty evaluator cache")
    code = supervisor.main(argv)
    audit = audit_attempts(args.run_dir)
    print("Audited logical invocations:", audit["total_started_logical_calls"],
          "including interrupted:", audit["interrupted_calls"])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
