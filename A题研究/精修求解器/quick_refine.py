#!/usr/bin/env python3
"""Extra-budget rapid P2/P3 exploration with a soft total time budget.

Uses frozen pending accounting, exact-plan deduplication and official evidence
checks. No baseline/operation/WCC phase and no fallback after initial failure.
The soft deadline is checked before generation and before each new candidate;
already-started generation/evaluation/verification may finish after it.
"""
import argparse
import copy
import json
import math
from pathlib import Path
import sys
import time
import traceback

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import controller as frozen


def source_hashes():
    return {**frozen.source_hashes(), str(Path(__file__).resolve()): frozen.digest(__file__)}


class SoftBudgetReached(Exception):
    pass


class QuickRefiner(frozen.Solver):
    """Own run loop; the frozen Solver's from-scratch/fallback loop is unused."""
    def __init__(self, graph, num_cores, problem, *, incumbent_plan, run_dir,
                 config=None, evaluation_dir=None, trace_cap=None, cache_cap=None,
                 round_width=4, max_rounds=2, max_evaluations=9, seed=17,
                 timeout=30, time_budget=120, evaluation_fn=None,
                 trace_fn=None, cache_fn=None, clock_fn=None):
        if type(problem) is not int or problem not in (2, 3):
            raise ValueError("quick refinement supports P2/P3 only")
        if type(time_budget) not in (int, float) or not math.isfinite(time_budget) or time_budget <= 0:
            raise ValueError("time_budget must be finite and positive")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("per-evaluation timeout must be in (0,60] seconds")
        if incumbent_plan is None:
            raise ValueError("a provided incumbent plan is required")
        self._clock = clock_fn or time.perf_counter
        self._rapid_started = self._clock()
        self.time_budget = time_budget
        self._deadline = self._rapid_started + time_budget
        self._deadline_boundary = None
        trace_cap = (8 if problem == 2 else 4) if trace_cap is None else trace_cap
        cache_cap = (0 if problem == 2 else 4) if cache_cap is None else cache_cap
        generators = {key: fn for key, fn in (("trace", trace_fn), ("cache", cache_fn)) if fn is not None}
        super().__init__(graph, num_cores, problem, incumbent_plan=incumbent_plan,
            run_dir=run_dir, config=config, evaluation_dir=evaluation_dir,
            component_cap=0, operation_cap=0, selective_cap=0, wcc_cap=0,
            trace_cap=trace_cap, cache_cap=cache_cap, round_width=round_width,
            max_rounds=max_rounds, max_evaluations=max_evaluations,
            seed=seed, timeout=timeout, evaluation_fn=evaluation_fn, generators=generators)
        self.manifest.update(experiment="extra_budget_rapid_exploration", solver="quick_refine_v1",
            source_sha256=source_hashes(), formal_v2_unchanged=True, formal_v3_unchanged=True,
            stage_order=["required_initial", "rotating_trace_cache"],
            time_budget_seconds=time_budget, time_budget_kind="soft_boundary_checks",
            timeout_seconds=timeout,
            elapsed_scope="QuickRefiner construction through final verification; excludes Python import/CLI startup",
            time_budget_scope="before generation and each candidate; already-started work and final evidence IO may finish after deadline; not a hard wall limit",
            initial_failure_policy="stop immediately; never run fallback or another stage",
            scope="extra-budget rapid exploration; not part of formal fair-budget matrices",
            stopping="logical/stage/round caps or soft deadline; never stop just because one round stagnates",
            pruning="none; P2/P3 do not use P1 lower bounds",
            test_hooks_used=self.manifest["test_hooks_used"] or clock_fn is not None)
        frozen.atomic_json(self.run_dir / "manifest.json", self.manifest)
        self.checkpoint("rapid_initialized")

    def integrity(self):
        if source_hashes() != self.manifest["source_sha256"]:
            raise frozen.EvidenceError("rapid execution source hashes changed")
        for path, expected in ((self.graph_path, self.manifest["graph_sha256"]),
                               (self.config_path, self.manifest["config_sha256"]),
                               (self.incumbent_path, self.manifest["incumbent_file_sha256"])):
            if frozen.digest(path) != expected:
                raise frozen.EvidenceError("rapid input bytes changed: " + str(path))

    def snapshot(self, state):
        result = super().snapshot(state)
        elapsed = self._clock() - self._rapid_started
        result.update(elapsed_seconds=elapsed, remaining_soft_seconds=max(0.0, self.time_budget - elapsed),
            soft_budget_overrun_seconds=max(0.0, elapsed - self.time_budget),
            deadline_reached=elapsed >= self.time_budget, profile_completed=False,
            configured_matrix_completed=False)
        return result

    def boundary(self, label):
        if self._clock() >= self._deadline:
            self._deadline_boundary = label
            raise SoftBudgetReached(label)

    def trial(self, candidate, stage, round_index=-1, parent=None):
        # Delegate durable pending accounting and full raw verification. A time
        # stop before reservation does not invent a failed/charged evaluation.
        self.boundary("before_candidate:" + stage)
        return super().trial(candidate, stage, round_index, parent)

    def stage_round(self, stage, round_index):
        used = sum(t["stage"] == stage for t in self.trials)
        limit = min(self.caps[stage] - used, self.max_evaluations - len(self.trials), self.round_width)
        if limit <= 0:
            return
        self.boundary("before_generation:" + stage)
        self.integrity()
        parent = self.best["plan_sha256"]
        before = list(frozen.objective(self.best["record"]))
        raw = self.verify_success(self.best["record"], self.best["plan"], parent)
        self.boundary("after_trace_verification:" + stage)
        entry = dict(stage=stage, round=round_index, parent_plan_sha256=parent,
            before=before, proposal_limit=limit, evaluation_start=len(self.trials), completed=False)
        self.stages.append(entry)
        rejected_before = len(self.rejected)
        started = self._clock()
        try:
            try:
                candidates, diagnostics = self.generators[stage](self.ir, copy.deepcopy(self.best["plan"]), raw,
                    num_cores=self.num_cores, max_candidates=limit, round_index=round_index, seed=self.seed)
                if len(candidates) > limit:
                    raise ValueError("generator exceeded declared proposal cap")
                path = self.run_dir / "generation" / ("{:03d}_{}_r{}.json".format(len(self.stages) - 1, stage, round_index))
                frozen.atomic_json(path, {**entry, "diagnostics": diagnostics, "candidates": candidates})
                entry.update(generation_path=str(path), generation_sha256=frozen.digest(path),
                             candidate_count=len(candidates), diagnostics=diagnostics)
            except Exception:
                entry["error"] = traceback.format_exc()
                self.generation_failures.append(copy.deepcopy(entry))
                candidates = []
            entry["generation_seconds"] = self._clock() - started
            self.integrity()
            self.boundary("after_generation:" + stage)
            for candidate in candidates:
                if len(self.trials) >= self.max_evaluations:
                    break
                try:
                    self.trial(candidate, stage, round_index, parent)
                except frozen.EvidenceError:
                    raise
                except (ValueError, TypeError, KeyError):
                    self.rejected.append({"stage": stage, "round": round_index,
                        "name": candidate.get("name"), "error": traceback.format_exc()})
            entry["rejected_candidate_count"] = len(self.rejected) - rejected_before
            entry["completed"] = not entry.get("error") and not entry["rejected_candidate_count"]
            if not entry["completed"]:
                entry["stop_reason"] = "generation_failure" if entry.get("error") else "candidate_rejected"
        except SoftBudgetReached:
            entry["stop_reason"] = "soft_time_budget"
            raise
        finally:
            entry.update(evaluation_end=len(self.trials), after=list(frozen.objective(self.best["record"])))
            entry["improved"] = entry["after"] < before
            self.checkpoint("rapid_stage_complete" if entry["completed"] else "rapid_stage_incomplete")

    def run(self):
        profile_completed, integrity_ok = True, False
        stop, error = "round_limit", None
        try:
            self.trial({"name": "provided_incumbent", "plan": self.initial,
                "metadata": {"family": "initial", "reevaluated_problem": self.problem}}, "initial")
            if self.best is None:
                stop, profile_completed = "initial_not_feasible", False
            else:
                for round_index in range(self.max_rounds):
                    if len(self.trials) >= self.max_evaluations:
                        stop = "evaluation_cap"
                        break
                    if all(sum(t["stage"] == s for t in self.trials) >= self.caps[s] for s in ("trace", "cache")):
                        stop = "stage_caps"
                        break
                    self.boundary("before_round:" + str(round_index))
                    for stage in ("trace", "cache") if self.problem == 3 else ("trace",):
                        self.stage_round(stage, round_index)
                if len(self.trials) >= self.max_evaluations:
                    stop = "evaluation_cap"
        except SoftBudgetReached:
            stop, profile_completed = "soft_time_budget", False
        except Exception:
            stop, profile_completed, error = "controller_error", False, traceback.format_exc()
        # Even an intentionally partial best must pass the complete frozen raw
        # evidence check again. Final verification/serialization may overrun.
        try:
            self.integrity()
            if self.best:
                self.verify_success(self.best["record"], self.best["plan"], self.best["plan_sha256"])
            integrity_ok = True
        except Exception:
            stop, profile_completed, error = "integrity_or_verification_failure", False, traceback.format_exc()
        search_stop = stop
        # An exhausted round/call budget is not a completed profile when some
        # planned generation failed or proposed candidates were malformed.
        # Retain verified best, but mark it partial and require review.
        if self.generation_failures or self.rejected or error:
            profile_completed = False
            if error is None:
                stop = ("generation_and_candidate_failure" if self.generation_failures and self.rejected else
                        "generation_failure" if self.generation_failures else "candidate_rejected")
        result = self.checkpoint("soft_time_stopped" if stop == "soft_time_budget" else
                                 ("finished" if profile_completed else "aborted"))
        result.update(completed=profile_completed and integrity_ok,
            profile_completed=profile_completed and integrity_ok, configured_matrix_completed=False,
            stop_reason=stop, search_stop_reason=search_stop, deadline_boundary=self._deadline_boundary,
            source_and_input_hashes_verified=integrity_ok, controller_error=error,
            best_official_verified=bool(self.best and integrity_ok),
            requires_review=bool(self.generation_failures or self.rejected or error))
        if not integrity_ok:
            result.update(status="integrity_failure", best_retained_for_audit_only=True)
        if self.best:
            result.update(output=str(self.run_dir / "best.plan.json"),
                          output_sha256=frozen.digest(self.run_dir / "best.plan.json"))
        result["partial_result"] = bool(self.best and not result["profile_completed"])
        frozen.atomic_json(self.run_dir / "summary.json", result)
        frozen.atomic_json(self.run_dir / "checkpoint.json", result)
        return result


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("graph", type=Path)
    p.add_argument("-n", "--num-cores", type=int, choices=range(1, 6), required=True)
    p.add_argument("-p", "--problem", type=int, choices=(2, 3), required=True)
    p.add_argument("--incumbent-plan", type=Path, required=True)
    p.add_argument("--config", type=Path)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--evaluation-dir", type=Path)
    p.add_argument("--trace-cap", type=int, help="Default P2:8 / P3:4")
    p.add_argument("--cache-cap", type=int, help="Default P2:0 / P3:4")
    p.add_argument("--round-width", type=int, default=4)
    p.add_argument("--max-rounds", type=int, default=2)
    p.add_argument("--max-evaluations", type=int, default=9)
    p.add_argument("--seed", type=int, default=17)
    p.add_argument("--timeout", type=float, default=30, help="Per worker in (0,60] seconds; not total time")
    p.add_argument("--time-budget", type=float, default=120, help="Soft total budget; started work and final verification can overrun")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        search = QuickRefiner(**vars(args))
        result = search.run()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({k: result.get(k) for k in ("status", "completed", "profile_completed", "partial_result",
        "requires_review", "best_official_verified", "logical_calls", "stop_reason", "elapsed_seconds", "output")}, ensure_ascii=False))
    # A valid partial best is useful, but a soft stop must not look like a
    # completed profile to a caller that uses the process exit code.
    return 0 if result["completed"] and result["status"] == "success" and not result["requires_review"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
