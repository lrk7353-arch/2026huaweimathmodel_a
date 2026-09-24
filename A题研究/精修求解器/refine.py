#!/usr/bin/env python3
"""Extra-budget, monotone P2/P3 refinement with rotating trace/cache neighborhoods.

This is separate from frozen formal-v2 experiments. Only official evaluations
establish feasibility/performance; a stagnant round is not a stopping condition.
"""
import argparse
from collections import Counter
import copy
import gzip
import json
import math
from pathlib import Path
import sys
import time
import traceback

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
sys.path.insert(0, str(RESEARCH))
from advanced_solver.engine import source_hashes, check_fixed_config
from advanced_solver.trace_refine import generate_trace_candidates
from advanced_solver.cache_refine import generate_cache_candidates
from solver.common import DATA, OFFICIAL, atomic_json, digest, object_digest, read_json
from solver.evaluator import evaluate
from solver.graph_ir import GraphIR
from solver.plan import validate_plan


def frozen_sources():
    """Conservative execution dependency manifest, including this new controller."""
    return {**source_hashes(), str(Path(__file__).resolve()): digest(__file__)}


def key(record):
    metrics = record["metrics"]
    values = metrics["makespan"], metrics["data_movement_bytes"]["added_copy_bytes"]
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("nonfinite/negative or missing official objective")
    return values


def audit_safe(value):
    """Keep malformed-return diagnostics serializable without accepting them."""
    if isinstance(value, dict):
        return {str(k): audit_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [audit_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_nonfinite_number": repr(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"invalid_non_json_value": repr(value)}


def outside_official(path):
    resolved = Path(path).expanduser().resolve()
    official = DATA.parent.resolve()
    if resolved == official or official in resolved.parents:
        raise ValueError("generated artifacts must stay outside official attachments")
    return resolved


def checked_paths(graph, config, incumbent, run_dir, evaluation_dir):
    """Validate before writing, resolving symlinks and rejecting existing attempts."""
    graph, config, incumbent = (Path(p).expanduser().resolve() for p in (graph, config, incumbent))
    for source in (graph, config, incumbent):
        if not source.is_file():
            raise ValueError("required input is not a file: " + str(source))
    raw_run = Path(run_dir).expanduser()
    if raw_run.exists() or raw_run.is_symlink():
        raise ValueError("strict fresh run directory required; even an empty existing directory is rejected")
    run = outside_official(raw_run)
    if run.exists():
        raise ValueError("resolved run directory already exists")
    outputs = {run / name for name in ("manifest.json", "checkpoint.json", "summary.json", "best.plan.json")}
    sources = {graph, config, incumbent} | {Path(p) for p in frozen_sources()}
    if outputs & sources or run in sources:
        raise ValueError("output/input/source path collision")
    evaluations = outside_official(evaluation_dir) if evaluation_dir else run / "evaluations"
    if evaluations in outputs | sources or (evaluations.exists() and not evaluations.is_dir()):
        raise ValueError("evaluation directory aliases an input/output or is not a directory")
    if evaluations == run or any(evaluations == run / name or run / name in evaluations.parents
                                  for name in ("trials", "generation")):
        raise ValueError("evaluation storage collides with audit storage")
    # The wrapper writes these children. An existing symlink there can escape
    # the chosen cache root even after resolving that root itself.
    for child in ("attempts", "cache"):
        if (evaluations / child).is_symlink():
            raise ValueError("evaluation attempts/cache children must not be symlinks")
    cache_root = evaluations / "cache"
    if cache_root.is_dir():
        for child in cache_root.iterdir():
            if child.is_symlink() or (child.is_dir() and (child / "success.json").is_symlink()):
                raise ValueError("evaluation cache key/result paths must not be symlinks")
    return graph, config, incumbent, run, evaluations


class Refiner:
    def __init__(self, graph, num_cores, problem, *, incumbent_plan, run_dir,
                 config=None, evaluation_dir=None, trace_cap=30, cache_cap=18,
                 round_width=6, max_rounds=5, max_evaluations=49, seed=17,
                 timeout=60, evaluation_fn=None, trace_fn=None, cache_fn=None):
        if type(num_cores) is not int or num_cores not in range(1, 6) or problem not in (2, 3):
            raise ValueError("P2/P3 and 1..5 cores required")
        for name, value, minimum in (("trace_cap", trace_cap, 0), ("cache_cap", cache_cap, 0),
                                     ("round_width", round_width, 1), ("max_rounds", max_rounds, 1),
                                     ("max_evaluations", max_evaluations, 1)):
            if type(value) is not int or value < minimum:
                raise ValueError(name + " has an invalid integer value")
        if type(seed) is not int or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("integer seed and finite positive timeout required")
        self.graph_path, self.config_path, self.incumbent_path, self.run_dir, self.evaluation_dir = checked_paths(
            graph, config or Path(graph).expanduser().parent / "config.txt", incumbent_plan, run_dir, evaluation_dir)
        check_fixed_config(self.config_path)
        self.ir = GraphIR.from_path(self.graph_path)
        self.initial = read_json(self.incumbent_path)
        validate_plan(self.ir, self.initial)
        if len(self.initial["core_schedules"]) != num_cores:
            raise ValueError("incumbent core count differs from -n")
        self.num_cores, self.problem, self.seed = num_cores, problem, seed
        self.caps = {"trace": trace_cap, "cache": cache_cap if problem == 3 else 0}
        self.round_width, self.max_rounds, self.max_evaluations, self.timeout = round_width, max_rounds, max_evaluations, timeout
        self.evaluate = evaluation_fn or evaluate
        self.generators = {"trace": trace_fn or generate_trace_candidates, "cache": cache_fn or generate_cache_candidates}
        self.started = time.perf_counter()
        self.trials, self.rounds, self.generation_failures, self.duplicates, self.rejected_candidates = [], [], [], [], []
        self.best, self.best_official, self.seen = None, None, set()
        self.manifest = {
            "schema_version": 1, "experiment": "extra_budget_rotating_monotone_refinement",
            "formal_v2_unchanged": True,
            "graph_path": str(self.graph_path), "graph_sha256": digest(self.graph_path),
            "config_path": str(self.config_path), "config_sha256": digest(self.config_path),
            "incumbent_path": str(self.incumbent_path), "incumbent_file_sha256": digest(self.incumbent_path),
            "incumbent_plan_sha256": object_digest(self.initial), "source_sha256": frozen_sources(),
            "python": sys.version, "num_cores": num_cores, "problem": problem,
            "seed": seed, "requested_caps": {"trace": trace_cap, "cache": cache_cap},
            "effective_caps": self.caps, "round_width": round_width, "max_rounds": max_rounds,
            "max_evaluations": max_evaluations, "timeout": timeout,
            "evaluation_dir": str(self.evaluation_dir), "external_evaluation_cache": evaluation_dir is not None,
            "round_order": ["trace", "cache"] if problem == 3 else ["trace"],
            "counting": "Initial, failure and exact-cache-hit calls each reserve one logical call before evaluation; duplicate plans do not call. Pending is charged but its worker outcome is unknown.",
            "stopping": "Evaluation/stage caps and fixed round limit only; never stop for a stagnant or duplicate-only round. Initial failure or evidence change aborts refinement.",
            "objective": ["official success", "makespan", "added_copy_bytes"],
            "generation_scope": "Late/waiting-copy heuristics and cache event replay; not an exact dynamic critical path. No artificial waits, changed machine settings, or case-ID rules.",
        }
        self.run_dir.mkdir(parents=True, exist_ok=False)
        atomic_json(self.run_dir / "manifest.json", self.manifest)
        self.checkpoint("initialized")

    def integrity(self):
        if frozen_sources() != self.manifest["source_sha256"]:
            raise ValueError("execution source hashes changed during this run")
        for path, expected in ((self.graph_path, self.manifest["graph_sha256"]),
                               (self.config_path, self.manifest["config_sha256"]),
                               (self.incumbent_path, self.manifest["incumbent_file_sha256"])):
            if digest(path) != expected:
                raise ValueError("read input changed during run: " + str(path))

    def snapshot(self, state):
        returned = [t for t in self.trials if t["state"] == "returned"]
        statuses = Counter(t["record"]["status"] if t["state"] == "returned" else "pending" for t in self.trials)
        return {**self.manifest, "state": state, "completed": False,
                "status": "success" if self.best else "no_feasible_result", "best": self.best,
                "evaluations": self.trials, "rounds": self.rounds,
                "generation_failures": self.generation_failures, "duplicates": self.duplicates,
                "rejected_candidates": self.rejected_candidates,
                "logical_calls": len(self.trials), "returned_calls": len(returned),
                "pending_calls": len(self.trials) - len(returned), "status_counts": dict(statuses),
                "cache_hits": sum(bool(t["record"].get("cache_hit")) for t in returned),
                "confirmed_worker_calls": sum(t["record"].get("returncode") is not None and not t["record"].get("cache_hit", False) for t in returned),
                "worker_launch_unknown_calls": sum(t["state"] == "pending" or t.get("record", {}).get("status") == "wrapper_exception" for t in self.trials),
                "stage_calls": dict(Counter(t["stage"] for t in self.trials)),
                "elapsed_seconds": time.perf_counter() - self.started}

    def checkpoint(self, state):
        if self.best:
            atomic_json(self.run_dir / "best.plan.json", self.best["plan"])
        value = self.snapshot(state)
        atomic_json(self.run_dir / "checkpoint.json", value)
        return value

    def verify_success(self, record, plan_hash):
        """Bind raw official evidence to this exact input, plan and machine."""
        if record.get("status") != "success" or record.get("problem") != self.problem:
            raise ValueError("successful record problem mismatch")
        h = record["hashes"]
        expected = {"graph_sha256": self.manifest["graph_sha256"], "config_sha256": self.manifest["config_sha256"],
                    "plan_sha256": plan_hash, "problem": self.problem,
                    "official_py_sha256": {p.name: self.manifest["source_sha256"][str(p.resolve())] for p in sorted(OFFICIAL.glob("*.py"))},
                    "wrapper_sha256": self.manifest["source_sha256"][str((RESEARCH / "solver/evaluator.py").resolve())],
                    "worker_sha256": self.manifest["source_sha256"][str((RESEARCH / "solver/eval_worker.py").resolve())]}
        if any(h.get(k) != v for k, v in expected.items()):
            raise ValueError("official evidence input/plan/config/code hash mismatch")
        if digest(record["plan_path"]) != plan_hash or digest(record["result_path"]) != record["result_sha256"]:
            raise ValueError("official plan or compressed result bytes changed")
        with gzip.open(record["result_path"], "rt", encoding="utf-8") as handle:
            result = json.load(handle)
        if result["makespan"] != key(record)[0] or result["num_cores"] != self.num_cores:
            raise ValueError("official makespan or core count mismatch")
        if result["data_movement_bytes"]["added_copy_bytes"] != key(record)[1]:
            raise ValueError("official added bytes differ from selection metrics")
        if result.get("scene") != "B" or (self.problem == 3 and result.get("problem") != 3):
            raise ValueError("raw official result problem mismatch")
        if self.problem == 2 and result.get("problem", 2) != 2:
            raise ValueError("P2 record contains another problem's result")
        return result

    def trial(self, candidate, stage, round_index, generated_parent=None):
        plan = copy.deepcopy(candidate["plan"])
        validate_plan(self.ir, plan)
        if len(plan["core_schedules"]) != self.num_cores:
            raise ValueError("candidate core count mismatch")
        hashed = object_digest(plan)
        if hashed in self.seen:
            self.duplicates.append({"stage": stage, "round": round_index, "name": candidate["name"], "plan_sha256": hashed})
            return False
        if len(self.trials) >= self.max_evaluations:
            return False
        self.integrity()
        index = len(self.trials)
        plan_path = self.run_dir / "trials" / ("{:04d}.plan.json".format(index))
        atomic_json(plan_path, plan)
        row = {"index": index, "stage": stage, "round": round_index, "name": candidate["name"],
               "metadata": copy.deepcopy(candidate.get("metadata", {})), "plan_sha256": hashed,
               "plan_path": str(plan_path), "plan_file_sha256": digest(plan_path), "state": "pending",
               "parent_plan_sha256": generated_parent,
               "before_incumbent_sha256": self.best["plan_sha256"] if self.best else None,
               "before": list(key(self.best["record"])) if self.best else None}
        self.seen.add(hashed)
        self.trials.append(row)
        # The reservation is durable before a process can be launched. If killed
        # just before evaluate, this conservatively charges an unknown call.
        atomic_json(self.run_dir / "trials" / ("{:04d}.json".format(index)), row)
        self.checkpoint("evaluation_pending")
        try:
            record = self.evaluate(self.graph_path, plan, self.problem, self.evaluation_dir,
                                   timeout=self.timeout, config_path=self.config_path)
        except Exception:
            record = {"status": "wrapper_exception", "error": traceback.format_exc(), "cache_hit": False}
        row.update({"state": "returned", "record": record, "accepted": False})
        if record.get("status") == "success":
            try:
                raw = self.verify_success(record, hashed)
            except Exception:
                row["unverified_returned_record"] = audit_safe(record)
                record = {"status": "evidence_verification_failed", "error": traceback.format_exc(),
                          "cache_hit": record.get("cache_hit", False), "returncode": record.get("returncode")}
                row["record"] = record
            else:
                if self.best is None or key(record) < key(self.best["record"]):
                    row["accepted"] = True
                    self.best = {**copy.deepcopy(row), "plan": plan}
                    self.best_official = raw
        row["after"] = list(key(self.best["record"])) if self.best else None
        atomic_json(self.run_dir / "trials" / ("{:04d}.json".format(index)), row)
        self.checkpoint("searching")
        return True

    def stage_round(self, stage, round_index):
        used = sum(t["stage"] == stage for t in self.trials)
        count = min(self.round_width, self.caps[stage] - used, self.max_evaluations - len(self.trials))
        if count <= 0:
            return
        self.integrity()
        start = time.perf_counter()
        parent_hash = self.best["plan_sha256"]
        before = list(key(self.best["record"]))
        entry = {"stage": stage, "round": round_index, "parent_plan_sha256": parent_hash,
                 "before": before, "proposal_limit": count, "evaluation_start": len(self.trials)}
        # Evidence failure aborts the controller, not merely this generation.
        # Keep it outside the recoverable generator-exception branch.
        raw = self.verify_success(self.best["record"], parent_hash)
        try:
            candidates, diagnostics = self.generators[stage](self.ir, copy.deepcopy(self.best["plan"]), raw,
                num_cores=self.num_cores, max_candidates=count, round_index=round_index, seed=self.seed)
            if len(candidates) > count:
                raise ValueError("generator exceeded its declared proposal limit")
            payload = {**entry, "diagnostics": diagnostics, "candidates": candidates}
            path = self.run_dir / "generation" / ("{:02d}_{}.json".format(round_index, stage))
            atomic_json(path, payload)
            entry.update({"generation_path": str(path), "generation_sha256": digest(path),
                          "candidate_count": len(candidates), "diagnostics": diagnostics})
        except Exception:
            entry["error"] = traceback.format_exc()
            self.generation_failures.append(copy.deepcopy(entry))
            candidates = []
        entry["generation_seconds"] = time.perf_counter() - start
        for candidate in candidates:
            if len(self.trials) >= self.max_evaluations:
                break
            try:
                self.trial(candidate, stage, round_index, generated_parent=parent_hash)
            except (ValueError, TypeError, KeyError):
                self.rejected_candidates.append({"stage": stage, "round": round_index,
                    "name": candidate.get("name"), "error": traceback.format_exc()})
                # Source changes must abort, not be treated as just an invalid candidate.
                self.integrity()
        entry.update({"after": list(key(self.best["record"])), "evaluation_end": len(self.trials),
                      "improved": list(key(self.best["record"])) < before})
        self.rounds.append(entry)
        self.checkpoint("round_complete")

    def run(self):
        stop, completed, integrity_valid = "round_limit", True, True
        try:
            self.trial({"name": "provided_incumbent", "plan": self.initial,
                        "metadata": {"family": "initial", "reevaluated_for_problem": self.problem}}, "initial", -1)
            if self.best is None:
                stop, completed = "initial_not_feasible", False
            else:
                for round_index in range(self.max_rounds):
                    if len(self.trials) >= self.max_evaluations:
                        stop = "evaluation_cap"
                        break
                    if all(sum(t["stage"] == s for t in self.trials) >= cap for s, cap in self.caps.items()):
                        stop = "stage_caps"
                        break
                    for stage in self.manifest["round_order"]:
                        self.stage_round(stage, round_index)
                if len(self.trials) >= self.max_evaluations:
                    stop = "evaluation_cap"
            self.integrity()
            if self.best:
                self.verify_success(self.best["record"], self.best["plan_sha256"])
        except Exception:
            stop, completed, integrity_valid = "controller_error", False, False
            self.generation_failures.append({"stage": "controller", "error": traceback.format_exc()})
        out = self.checkpoint("finished" if completed else "aborted")
        out.update({"completed": completed, "stop_reason": stop, "source_and_input_hashes_verified": integrity_valid})
        if not integrity_valid:
            out["status"] = "integrity_failure"
            out["best_retained_for_audit_only"] = True
        if self.best:
            out["best_plan_file_sha256"] = digest(self.run_dir / "best.plan.json")
        atomic_json(self.run_dir / "summary.json", out)
        atomic_json(self.run_dir / "checkpoint.json", out)
        return out


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("graph", type=Path)
    p.add_argument("-n", "--num-cores", type=int, choices=range(1, 6), required=True)
    p.add_argument("-p", "--problem", type=int, choices=(2, 3), required=True)
    p.add_argument("--incumbent-plan", type=Path, required=True)
    p.add_argument("--config", type=Path)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--evaluation-dir", type=Path)
    p.add_argument("--trace-cap", type=int, default=30)
    p.add_argument("--cache-cap", type=int, default=18)
    p.add_argument("--round-width", type=int, default=6)
    p.add_argument("--max-rounds", type=int, default=5)
    p.add_argument("--max-evaluations", type=int, default=49)
    p.add_argument("--seed", type=int, default=17)
    p.add_argument("--timeout", type=float, default=60)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        search = Refiner(**vars(args))
        result = search.run()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps({"status": result["status"], "completed": result["completed"],
        "logical_calls": result["logical_calls"], "stop_reason": result["stop_reason"],
        "best": list(key(result["best"]["record"])) if result["best"] else None,
        "summary": str(search.run_dir / "summary.json")}, ensure_ascii=False))
    return 0 if result["status"] == "success" and result["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
