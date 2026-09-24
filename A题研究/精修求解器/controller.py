"""Audited graph-to-plan portfolio; independent of the frozen formal-v2 engine."""
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
sys.path[:0] = [str(HERE), str(RESEARCH)]
import refine as refinement_helpers
from advanced_solver.component_baseline import generate_component_candidates
from advanced_solver.operation_assign import generate_operation_candidates
from advanced_solver.trace_refine import generate_trace_candidates
from advanced_solver.cache_refine import generate_cache_candidates
from solver.common import DATA, OFFICIAL, atomic_json, digest, object_digest, read_json, single_active_plan
from solver.evaluator import evaluate
from solver.graph_ir import GraphIR
from solver.plan import validate_plan
from p1_selective import generate_selective_candidates, task_lower_bound
from wcc_interleave_v2 import generate_interleave_candidates as protected_interleave
from wcc_interleave import generate_interleave_candidates as unrestricted_interleave


def source_hashes():
    # refine imports the frozen engine/helpers, so its conservative dependency
    # closure is included too. Never glob this actively developed directory.
    sources = refinement_helpers.frozen_sources()
    for name in ("controller.py", "solve.py", "p1_selective.py", "wcc_interleave.py", "wcc_interleave_v2.py"):
        path = HERE / name
        sources[str(path.resolve())] = digest(path)
    return sources


def objective(record):
    metrics = record["metrics"]
    makespan = metrics["makespan"]
    added = metrics["data_movement_bytes"]["added_copy_bytes"]
    if type(makespan) not in (int, float) or not math.isfinite(makespan) or makespan < 0:
        raise ValueError("invalid official makespan")
    # This is an official difference, scheduled - original bytes. Do not
    # silently clamp a signed value or supply a missing secondary objective.
    if type(added) is not int:
        raise ValueError("official added_copy_bytes must be an integer")
    return makespan, added


class EvidenceError(ValueError):
    pass


def generate_wcc_candidates(ir, plan, *, num_cores, max_candidates=9, seed=17, policy="mixed"):
    """Control first, then alternate protected/unrestricted unique plans."""
    if policy not in ("mixed", "protected", "unrestricted"):
        raise ValueError("unknown WCC policy")
    families = ("protected", "unrestricted") if policy == "mixed" else (policy,)
    generators = {"protected": protected_interleave, "unrestricted": unrestricted_interleave}
    pools, diagnostics = {}, {}
    for family in families:
        pools[family], diagnostics[family] = generators[family](ir, plan, num_cores=num_cores,
                                                               max_candidates=max_candidates, seed=seed)
    rows, unique, all_origins = [], {}, {}
    # Preserve origins even when their alias comes after the selected prefix.
    for family in families:
        for value in pools[family]:
            h = object_digest(value["plan"])
            all_origins.setdefault(h, []).append({"variant": family, "name": value["name"]})
    controls = [value for family in families for value in pools[family]
                if value["metadata"].get("strategy") == "reencode_control"]
    if len({object_digest(v["plan"]) for v in controls}) > 1:
        raise ValueError("WCC variants disagree on the shared reencoding control")
    def emit(family, value):
        h = object_digest(value["plan"])
        if h in unique or len(rows) >= max_candidates:
            return
        row = copy.deepcopy(value)
        row["name"] = "wcc_" + family + "_" + row["name"]
        row["metadata"].update(wcc_policy=policy, actual_variant=family, variant_origins=all_origins[h])
        unique[h] = row
        rows.append(row)
    for family in families:
        for value in pools[family]:
            if value["metadata"].get("strategy") == "reencode_control":
                emit(family, value)
    remaining = {f: [v for v in pools[f] if v["metadata"].get("strategy") != "reencode_control"] for f in families}
    cursors = {f: 0 for f in families}
    while len(rows) < max_candidates:
        advanced = False
        for family in families:
            while cursors[family] < len(remaining[family]):
                value = remaining[family][cursors[family]]
                cursors[family] += 1
                if object_digest(value["plan"]) in unique:
                    continue
                emit(family, value)
                advanced = True
                break
        if not advanced:
            break
    return rows, {"policy": policy, "family_diagnostics": diagnostics,
                  "generated_unique": len(all_origins), "selected": len(rows),
                  "omitted_plan_sha256": [h for h in all_origins if h not in unique],
                  "selection": "shared control first; protected/unrestricted alternating unique plans, exhaust duplicates and fill from remaining family; no official-score ranking"}


def checked_paths(graph, config, incumbent, run_dir, evaluation_dir, output):
    sources = [Path(graph).expanduser().resolve(), Path(config).expanduser().resolve()]
    if incumbent is not None:
        sources.append(Path(incumbent).expanduser().resolve())
    for path in sources:
        if not path.is_file():
            raise ValueError("required input is not a file: " + str(path))
    raw_run = Path(run_dir).expanduser()
    if raw_run.exists() or raw_run.is_symlink():
        raise ValueError("strict fresh run directory required, including no preexisting empty directory")
    run = refinement_helpers.outside_official(raw_run)
    if run.exists():
        raise ValueError("resolved run directory already exists")
    output_raw = Path(output).expanduser() if output else run / "best.plan.json"
    if output_raw.exists() or output_raw.is_symlink():
        raise ValueError("output must be a fresh file path")
    out = refinement_helpers.outside_official(output_raw)
    for ancestor in out.parents:
        if ancestor.exists() and not ancestor.is_dir():
            raise ValueError("output parent is not a directory")
    protected = set(sources) | {Path(p) for p in source_hashes()}
    audit_files = {run / n for n in ("manifest.json", "checkpoint.json", "summary.json")}
    if out in protected | audit_files or out == run or out.suffix != ".json":
        raise ValueError("output aliases input/source/audit or is not a .json path")
    reserved_files = protected | audit_files | {run / "best.plan.json"}
    if any(f in out.parents or out in f.parents for f in reserved_files):
        raise ValueError("output overlaps a reserved file ancestor/descendant")
    for name in ("trials", "generation", "evaluations"):
        if out == run / name or run / name in out.parents:
            raise ValueError("output collides with audit/evaluation storage")
    evaluations = refinement_helpers.outside_official(evaluation_dir) if evaluation_dir else run / "evaluations"
    if evaluations in protected | audit_files | {out, run, run / "best.plan.json"}:
        raise ValueError("evaluation directory aliases protected storage")
    if any(f in evaluations.parents or evaluations in f.parents for f in reserved_files):
        raise ValueError("evaluation directory overlaps a reserved file ancestor/descendant")
    if evaluations.exists() and not evaluations.is_dir():
        raise ValueError("evaluation directory is not a directory")
    if evaluations in out.parents or out in evaluations.parents:
        raise ValueError("output and evaluation storage overlap")
    for name in ("trials", "generation"):
        root = run / name
        if evaluations == root or root in evaluations.parents:
            raise ValueError("evaluation directory collides with audit subtree")
    for child in ("attempts", "cache"):
        if (evaluations / child).is_symlink():
            raise ValueError("evaluation write subtrees must not be symlinks")
    if (evaluations / "cache").is_dir():
        for child in (evaluations / "cache").iterdir():
            if child.is_symlink() or (child.is_dir() and (child / "success.json").is_symlink()):
                raise ValueError("evaluation cache keys/results must not be symlinks")
    return sources[0], sources[1], sources[2] if incumbent is not None else None, run, evaluations, out


class Solver:
    def __init__(self, graph, num_cores, problem, *, run_dir, config=None,
                 evaluation_dir=None, incumbent_plan=None, output=None,
                 component_cap=6, operation_cap=12, selective_cap=18, wcc_cap=9,
                 trace_cap=30, cache_cap=18, round_width=6, max_rounds=5,
                 max_evaluations=90, seed=17, timeout=None, wcc_policy="mixed", evaluation_fn=None,
                 generators=None, lower_bound_fn=None):
        if type(num_cores) is not int or num_cores not in range(1, 6) or type(problem) is not int or problem not in (1, 2, 3):
            raise ValueError("problem 1..3 and num_cores 1..5 required")
        caps = dict(component=component_cap, operation=operation_cap, selective=selective_cap,
                    wcc=wcc_cap, trace=trace_cap, cache=cache_cap)
        if any(type(v) is not int or v < 0 for v in caps.values()):
            raise ValueError("stage caps must be nonnegative integers")
        if any(type(v) is not int or v < 1 for v in (round_width, max_rounds, max_evaluations)):
            raise ValueError("round width, round count and total call cap must be positive integers")
        if type(seed) is not int or (timeout is not None and
                (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0)):
            raise ValueError("integer seed and finite positive timeout required")
        if wcc_policy not in ("mixed", "protected", "unrestricted"):
            raise ValueError("unknown WCC policy")
        self.graph_path, self.config_path, self.incumbent_path, self.run_dir, self.evaluation_dir, self.output = checked_paths(
            graph, config or Path(graph).expanduser().parent / "config.txt", incumbent_plan, run_dir, evaluation_dir, output)
        refinement_helpers.check_fixed_config(self.config_path)
        self.ir = GraphIR.from_path(self.graph_path)
        requested_timeout = timeout
        timeout = timeout if timeout is not None else (60 if len(self.ir.compute_ids) <= 10000 else 180)
        self.initial = read_json(self.incumbent_path) if self.incumbent_path else None
        if self.initial is not None:
            validate_plan(self.ir, self.initial)
            if len(self.initial["core_schedules"]) != num_cores:
                raise ValueError("incumbent core count differs from requested cores")
        self.num_cores, self.problem, self.seed = num_cores, problem, seed
        self.wcc_policy = wcc_policy
        self.caps = {s: n if (s == "component" or (problem == 1 and s == "selective") or
                     (problem in (2, 3) and s in ("operation", "wcc", "trace")) or
                     (problem == 3 and s == "cache")) else 0 for s, n in caps.items()}
        self.round_width, self.max_rounds, self.max_evaluations, self.timeout = round_width, max_rounds, max_evaluations, timeout
        self.evaluate = evaluation_fn or evaluate
        self.generators = {"component": generate_component_candidates, "operation": generate_operation_candidates,
            "selective": generate_selective_candidates, "wcc": generate_wcc_candidates,
            "trace": generate_trace_candidates, "cache": generate_cache_candidates}
        if generators:
            if set(generators) - set(self.generators):
                raise ValueError("unknown generator stage")
            self.generators.update(generators)
        self.lower_bound = lower_bound_fn or task_lower_bound
        self.trials, self.stages, self.duplicates, self.pruned, self.generation_failures, self.rejected = [], [], [], [], [], []
        self.seen, self.best, self.started = {}, None, time.perf_counter()
        self.manifest = {"schema_version": 1, "solver": "delivery_portfolio_v1", "formal_v2_unchanged": True,
            "graph_path": str(self.graph_path), "graph_sha256": digest(self.graph_path),
            "config_path": str(self.config_path), "config_sha256": digest(self.config_path),
            "incumbent_path": str(self.incumbent_path) if self.incumbent_path else None,
            "incumbent_file_sha256": digest(self.incumbent_path) if self.incumbent_path else None,
            "incumbent_plan_sha256": object_digest(self.initial) if self.initial is not None else None,
            "source_sha256": source_hashes(), "python": sys.version,
            "num_cores": num_cores, "problem": problem, "seed": seed, "wcc_policy": wcc_policy,
            "requested_caps": caps, "effective_caps": self.caps, "round_width": round_width,
            "max_rounds": max_rounds, "max_evaluations": max_evaluations, "timeout": timeout,
            "requested_timeout": requested_timeout,
            "evaluation_dir": str(self.evaluation_dir), "output": str(self.output),
            "external_evaluation_cache": evaluation_dir is not None,
            "stage_order": ["optional_initial", "component", "fallback_if_no_best", "selective" if problem == 1 else "operation"]
                       + (["wcc", "rotating_trace_cache"] if problem in (2, 3) else []),
            "budget_semantics": "Each initial, fallback, failure and cache hit reserves one logical call before evaluation. Exact repeats and certified P1 strict-lower-bound rejections do not call. Generation is bounded by its requested proposal count.",
            "stopping": "Caps and round limit; no stopping for one stagnant or duplicate-only round. Integrity change aborts.",
            "objective": ["official_success", "makespan", "added_copy_bytes"],
            "pruning": "P1 selective only: recomputed plan-specific necessary lower bound strictly exceeds incumbent makespan; equality is evaluated.",
            "validation_scope": "Engineering defaults, not a fully validated equal-budget benchmark. No historical result lookup or case-ID branch.",
            "test_hooks_used": evaluation_fn is not None or bool(generators) or lower_bound_fn is not None}
        self.run_dir.mkdir(parents=True, exist_ok=False)
        atomic_json(self.run_dir / "manifest.json", self.manifest)
        self.checkpoint("initialized")

    def integrity(self):
        if source_hashes() != self.manifest["source_sha256"]:
            raise EvidenceError("execution dependency sources changed")
        for path, expected in ((self.graph_path, self.manifest["graph_sha256"]),
                               (self.config_path, self.manifest["config_sha256"]),
                               (self.incumbent_path, self.manifest["incumbent_file_sha256"])):
            if path is not None and digest(path) != expected:
                raise EvidenceError("input changed: " + str(path))

    def verify_success(self, record, plan, plan_hash):
        validate_plan(self.ir, plan)
        if len(plan["core_schedules"]) != self.num_cores:
            raise EvidenceError("verified plan core count mismatch")
        if record.get("status") != "success" or record.get("problem") != self.problem or record.get("returncode") != 0:
            raise EvidenceError("record status/problem mismatch")
        hashes = record["hashes"]
        source = self.manifest["source_sha256"]
        expected = {"graph_sha256": self.manifest["graph_sha256"], "plan_sha256": plan_hash,
            "config_sha256": self.manifest["config_sha256"], "problem": self.problem,
            "official_py_sha256": {p.name: source[str(p.resolve())] for p in sorted(OFFICIAL.glob("*.py"))},
            "wrapper_sha256": source[str((RESEARCH / "solver/evaluator.py").resolve())],
            "worker_sha256": source[str((RESEARCH / "solver/eval_worker.py").resolve())]}
        if any(hashes.get(k) != v for k, v in expected.items()):
            raise EvidenceError("record graph/plan/config/implementation hashes mismatch")
        if object_digest(plan) != plan_hash or digest(record["plan_path"]) != plan_hash:
            raise EvidenceError("exact plan bytes mismatch")
        if digest(record["result_path"]) != record["result_sha256"]:
            raise EvidenceError("official gzip bytes mismatch")
        with gzip.open(record["result_path"], "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        expected_raw = {"scene": "A" if self.problem == 1 else "B", "num_cores": self.num_cores,
            "bandwidth_bytes_per_cycle": 60, "capacity_bytes": {"L1": 524288, "UB": 131072}}
        if self.problem == 1:
            expected_raw.update(task_cross_core_wait_cycles=1000, task_same_core_wait_cycles=100)
        else:
            expected_raw.update(cross_core_copy_delay_cycles=500)
        if self.problem == 3:
            expected_raw.update(problem=3, cache_mode="read_only", cache_capacity_bytes=1048576,
                                cache_bandwidth_bytes_per_cycle=250)
        if (type(raw.get("num_cores")) is not int or any(raw.get(k) != v for k, v in expected_raw.items()) or
                raw.get("problem", self.problem) != self.problem):
            raise EvidenceError("raw official scene/core/config mismatch")
        # Exact-cache keys bind graph BYTES, not basenames. A renamed byte-
        # identical graph may legitimately reuse a gzip naming its old source.
        if (not isinstance(raw.get("input_graph"), str) or not raw["input_graph"] or
                (not record.get("cache_hit") and raw["input_graph"] != self.graph_path.name) or
                raw.get("input_plan") != Path(record["plan_path"]).name):
            raise EvidenceError("raw official input provenance mismatch")
        if raw["makespan"] != objective(record)[0] or raw["data_movement_bytes"] != record["metrics"]["data_movement_bytes"]:
            raise EvidenceError("raw official objective/data movement differ from record")
        if record["metrics"].get("num_cores") != self.num_cores:
            raise EvidenceError("record metric core count mismatch")
        if self.problem == 3 and raw["cache_stats"] != record["metrics"].get("cache_stats"):
            raise EvidenceError("raw P3 cache statistics differ from record")
        mapping = {int(o): sg for o, sg in plan["node_to_subgraph"].items()}
        by_sg = {sg: c for c, seq in enumerate(plan["core_schedules"]) for sg in seq}
        timelines = raw["per_core_timeline"]
        if (len(timelines) != self.num_cores or any(type(c["core_id"]) is not int for c in timelines) or
                {c["core_id"] for c in timelines} != set(range(self.num_cores))):
            raise EvidenceError("raw timeline core coverage mismatch")
        found, finish = set(), 0
        for core in timelines:
            c = core["core_id"]
            tasks = core.get("tasks", [])
            encoded_order = ([t["subgraph_id"] for t in tasks] if self.problem == 1 else
                             [sg for t in tasks for sg in t["subgraph_ids"]])
            if encoded_order != plan["core_schedules"][c]:
                raise EvidenceError("official Task/subgraph core order mismatch")
            for op in core["ops"]:
                start, end = op["start"], op["end"]
                if any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end)) or start < 0 or end < start:
                    raise EvidenceError("invalid official timeline time")
                finish = max(finish, end)
                ident = op["op_id"]
                if ident not in mapping:
                    continue
                original = self.ir.ops[ident]
                sg = op["task_id"] if self.problem == 1 else op["subgraph_id"]
                if ident in found or sg != mapping[ident] or c != by_sg[sg] or op["op"] != original["op"] or op["pipe"] != original["pipe"]:
                    raise EvidenceError("raw original compute plan/core identity mismatch")
                if end - start != max(1, original["cycles"]):
                    raise EvidenceError("raw compute duration differs from original")
                found.add(ident)
        if found != set(mapping) or finish != raw["makespan"]:
            raise EvidenceError("raw compute coverage or final makespan mismatch")
        return raw

    def snapshot(self, state):
        rows = [t for t in self.trials if t["state"] == "returned"]
        return {**self.manifest, "state": state, "completed": False,
            "status": "success" if self.best else "no_feasible_result", "best": self.best,
            "evaluations": self.trials, "stages": self.stages, "duplicates": self.duplicates,
            "pruned": self.pruned, "generation_failures": self.generation_failures,
            "rejected_candidates": self.rejected, "logical_calls": len(self.trials),
            "returned_calls": len(rows), "pending_calls": len(self.trials) - len(rows),
            "stage_calls": dict(Counter(t["stage"] for t in self.trials)),
            "status_counts": dict(Counter(t["record"]["status"] if t["state"] == "returned" else "pending" for t in self.trials)),
            "cache_hits": sum(bool(t["record"].get("cache_hit")) for t in rows),
            "confirmed_worker_calls": sum(t["record"].get("returncode") is not None and not t["record"].get("cache_hit", False) for t in rows),
            "worker_launch_unknown_calls": sum(t["state"] == "pending" or t.get("record", {}).get("status") == "wrapper_exception" for t in self.trials),
            "elapsed_seconds": time.perf_counter() - self.started}

    def checkpoint(self, state):
        if self.best:
            atomic_json(self.run_dir / "best.plan.json", self.best["plan"])
        value = self.snapshot(state)
        atomic_json(self.run_dir / "checkpoint.json", value)
        return value

    def trial(self, candidate, stage, round_index=-1, parent=None):
        plan = copy.deepcopy(candidate["plan"])
        validate_plan(self.ir, plan)
        if len(plan["core_schedules"]) != self.num_cores:
            raise ValueError("candidate core count mismatch")
        signature = object_digest(plan)
        basic = {"stage": stage, "round": round_index, "name": candidate["name"], "plan_sha256": signature,
                 "parent_plan_sha256": parent, "before_incumbent_sha256": self.best["plan_sha256"] if self.best else None}
        if signature in self.seen:
            self.duplicates.append({**basic, "first_decision": self.seen[signature]})
            return False
        if len(self.trials) >= self.max_evaluations:
            return False
        self.integrity()
        if self.problem == 1 and stage == "selective" and self.best:
            certificate = self.lower_bound(self.ir, plan)
            if certificate["value"] > objective(self.best["record"])[0]:
                self.seen[signature] = "strict_p1_lower_bound_prune"
                self.pruned.append({**basic, "certificate": certificate,
                                    "incumbent_objective": list(objective(self.best["record"]))})
                self.checkpoint("candidate_pruned")
                return False
        index = len(self.trials)
        plan_path = self.run_dir / "trials" / ("{:04d}.plan.json".format(index))
        atomic_json(plan_path, plan)
        row = {**basic, "index": index, "metadata": copy.deepcopy(candidate.get("metadata", {})),
               "plan_path": str(plan_path), "plan_file_sha256": digest(plan_path), "state": "pending",
               "before": list(objective(self.best["record"])) if self.best else None}
        self.seen[signature] = "evaluation_reserved"
        self.trials.append(row)
        atomic_json(self.run_dir / "trials" / ("{:04d}.json".format(index)), row)
        self.checkpoint("evaluation_pending")
        try:
            record = self.evaluate(self.graph_path, plan, self.problem, self.evaluation_dir,
                                   config_path=self.config_path, timeout=self.timeout)
            if not isinstance(record, dict) or not isinstance(record.get("status"), str):
                raise ValueError("evaluator returned malformed record")
        except Exception:
            record = {"status": "wrapper_exception", "error": traceback.format_exc(), "cache_hit": False}
        row.update(state="returned", record=refinement_helpers.audit_safe(record), accepted=False)
        if record.get("status") == "success":
            try:
                self.verify_success(record, plan, signature)
            except Exception:
                row["unverified_returned_record"] = refinement_helpers.audit_safe(record)
                row["record"] = {"status": "evidence_verification_failed", "error": traceback.format_exc(),
                                 "cache_hit": record.get("cache_hit", False), "returncode": record.get("returncode")}
            else:
                if self.best is None or objective(record) < objective(self.best["record"]):
                    row["accepted"] = True
                    self.best = {**copy.deepcopy(row), "plan": plan}
        row["after"] = list(objective(self.best["record"])) if self.best else None
        atomic_json(self.run_dir / "trials" / ("{:04d}.json".format(index)), row)
        self.checkpoint("searching")
        return True

    def stage(self, stage, round_index=-1, iterative=False):
        used = sum(t["stage"] == stage for t in self.trials)
        limit = min(self.caps[stage] - used, self.max_evaluations - len(self.trials))
        if iterative:
            limit = min(limit, self.round_width)
        if limit <= 0:
            return
        dependent = stage in ("wcc", "trace", "cache")
        if dependent and self.best is None:
            self.stages.append({"stage": stage, "round": round_index, "skipped": "no_verified_incumbent"})
            return
        self.integrity()
        parent = self.best["plan_sha256"] if dependent else None
        before = list(objective(self.best["record"])) if self.best else None
        start = time.perf_counter()
        entry = {"stage": stage, "round": round_index, "parent_plan_sha256": parent,
                 "before": before, "proposal_limit": limit, "evaluation_start": len(self.trials)}
        raw = self.verify_success(self.best["record"], self.best["plan"], parent) if dependent else None
        try:
            kwargs = dict(num_cores=self.num_cores, max_candidates=limit, seed=self.seed)
            args = [self.ir]
            if dependent:
                args.append(copy.deepcopy(self.best["plan"]))
            if stage in ("trace", "cache"):
                args.append(raw)
                kwargs["round_index"] = round_index
            if stage == "wcc":
                kwargs["policy"] = self.wcc_policy
            candidates, diagnostics = self.generators[stage](*args, **kwargs)
            if len(candidates) > limit:
                raise ValueError("generator exceeded proposal cap")
            generation = self.run_dir / "generation" / ("{:03d}_{}_r{}.json".format(len(self.stages), stage, round_index))
            atomic_json(generation, {**entry, "diagnostics": diagnostics, "candidates": candidates})
            entry.update(generation_path=str(generation), generation_sha256=digest(generation),
                         candidate_count=len(candidates), diagnostics=diagnostics)
        except Exception:
            entry["error"] = traceback.format_exc()
            self.generation_failures.append(copy.deepcopy(entry))
            candidates = []
        entry["generation_seconds"] = time.perf_counter() - start
        self.integrity()
        for candidate in candidates:
            if len(self.trials) >= self.max_evaluations:
                break
            try:
                self.trial(candidate, stage, round_index, parent)
            except EvidenceError:
                raise
            except (ValueError, TypeError, KeyError):
                self.rejected.append({"stage": stage, "round": round_index,
                                      "name": candidate.get("name"), "error": traceback.format_exc()})
        entry.update(evaluation_end=len(self.trials), after=list(objective(self.best["record"])) if self.best else None)
        entry["improved"] = entry["after"] is not None and (before is None or entry["after"] < before)
        self.stages.append(entry)
        self.checkpoint("stage_complete")

    def run(self):
        completed, stop, error = True, "portfolio_exhausted", None
        try:
            if self.initial is not None:
                self.trial({"name": "provided_incumbent", "plan": self.initial,
                            "metadata": {"family": "initial", "reevaluated_problem": self.problem}}, "initial")
            self.stage("component")
            if self.best is None and len(self.trials) < self.max_evaluations:
                self.trial({"name": "single_active_fallback", "plan": single_active_plan(self.ir.graph, self.num_cores),
                            "metadata": {"family": "fallback"}}, "fallback")
            self.stage("selective" if self.problem == 1 else "operation")
            if self.problem in (2, 3):
                self.stage("wcc")
                for r in range(self.max_rounds):
                    if len(self.trials) >= self.max_evaluations:
                        break
                    for stage in ("trace", "cache") if self.problem == 3 else ("trace",):
                        self.stage(stage, r, iterative=True)
                stop = "round_limit_or_stage_caps"
            if len(self.trials) >= self.max_evaluations:
                stop = "evaluation_cap"
            self.integrity()
            if self.best:
                self.verify_success(self.best["record"], self.best["plan"], self.best["plan_sha256"])
        except Exception:
            completed, stop, error = False, "controller_error", traceback.format_exc()
        result = self.checkpoint("finished" if completed else "aborted")
        result.update(completed=completed, stop_reason=stop, integrity_verified=completed,
                      source_and_input_hashes_verified=completed, controller_error=error)
        result["requires_review"] = bool(self.generation_failures or self.rejected or error)
        if not completed:
            result.update(status="integrity_or_controller_failure", best_retained_for_audit_only=True)
        elif self.best:
            # Path was reserved before search; do not silently replace a file
            # another process created during evaluation.
            if self.output.resolve() != self.output or (self.output != self.run_dir / "best.plan.json" and
                                                        (self.output.exists() or self.output.is_symlink())):
                result.update(completed=False, state="aborted", stop_reason="output_collision",
                              controller_error="output path appeared during search; preserved it")
            else:
                atomic_json(self.output, self.best["plan"])
                result.update(output=str(self.output), output_sha256=digest(self.output))
        atomic_json(self.run_dir / "summary.json", result)
        atomic_json(self.run_dir / "checkpoint.json", result)
        return result


def verify_summary(path, expected_settings=None, require_completed=True):
    """Read-only evidence audit for batch resume; never calls a generator/evaluator.

    expected_settings compares explicit top-level manifest keys, including the
    full requested_caps dict when supplied. Failed/unfinished summaries cannot
    be treated as completed successful slots by the default verifier.
    """
    from types import SimpleNamespace
    path = Path(path).resolve()
    summary = read_json(path)
    if require_completed and (not summary.get("completed") or summary.get("status") != "success" or
                              not summary.get("source_and_input_hashes_verified") or summary.get("requires_review")):
        raise EvidenceError("resume requires a completed, verified successful summary")
    if summary.get("test_hooks_used"):
        raise EvidenceError("test-hook runs are not production resume evidence")
    if summary.get("source_sha256") != source_hashes():
        raise EvidenceError("summary execution source manifest differs from current sources")
    for setting, value in (expected_settings or {}).items():
        if summary.get(setting) != value:
            raise EvidenceError("resume setting mismatch: " + setting)
    manifest = read_json(path.parent / "manifest.json")
    if any(summary.get(k) != v for k, v in manifest.items()):
        raise EvidenceError("summary and frozen manifest differ")
    for role, hashed in (("graph_path", "graph_sha256"), ("config_path", "config_sha256"),
                         ("incumbent_path", "incumbent_file_sha256")):
        if summary.get(role) is not None and digest(summary[role]) != summary[hashed]:
            raise EvidenceError("summary input bytes changed: " + role)
    ir = GraphIR.from_path(summary["graph_path"])
    # A read-only view carries only evidence context, not a partially initialized
    # running Solver or a bypass around Refiner's P2/P3 constructor.
    view = SimpleNamespace(ir=ir, manifest=summary, problem=summary["problem"],
                           num_cores=summary["num_cores"], graph_path=Path(summary["graph_path"]))
    rows = summary["evaluations"]
    if summary["logical_calls"] != len(rows) or (require_completed and any(r["state"] != "returned" for r in rows)):
        raise EvidenceError("logical call accounting or pending state mismatch")
    if len(rows) > summary["max_evaluations"]:
        raise EvidenceError("logical call cap exceeded")
    stage_counts = dict(Counter(r["stage"] for r in rows))
    if stage_counts != summary["stage_calls"] or any(stage_counts.get(s, 0) > limit for s, limit in summary["effective_caps"].items()):
        raise EvidenceError("stage call accounting or cap mismatch")
    if stage_counts.get("initial", 0) > 1 or stage_counts.get("fallback", 0) > 1:
        raise EvidenceError("initial/fallback call cap exceeded")
    best = None
    for index, row in enumerate(rows):
        if row["index"] != index or digest(row["plan_path"]) != row["plan_file_sha256"]:
            raise EvidenceError("trial index or archived plan file changed")
        plan = read_json(row["plan_path"])
        if object_digest(plan) != row["plan_sha256"]:
            raise EvidenceError("trial exact plan content changed")
        validate_plan(ir, plan)
        if row["state"] != "returned":
            continue
        if row["record"]["status"] == "success":
            Solver.verify_success(view, row["record"], plan, row["plan_sha256"])
            accept = best is None or objective(row["record"]) < objective(best["record"])
            if row["accepted"] != accept:
                raise EvidenceError("trial incumbent acceptance mismatch")
            if accept:
                best = row
        elif row.get("accepted"):
            raise EvidenceError("failed trial accepted as incumbent")
    if summary.get("best"):
        stored = summary["best"]
        if best is None or stored["plan_sha256"] != best["plan_sha256"] or stored["record"] != best["record"]:
            raise EvidenceError("summary best differs from verified trial prefix")
        Solver.verify_success(view, stored["record"], stored["plan"], stored["plan_sha256"])
        if digest(summary["output"]) != summary["output_sha256"] or object_digest(read_json(summary["output"])) != stored["plan_sha256"]:
            raise EvidenceError("published plan differs from verified best")
    elif best is not None:
        raise EvidenceError("verified best missing from summary")
    return {**summary, "verified": True}
