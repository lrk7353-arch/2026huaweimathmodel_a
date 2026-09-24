"""Unified staged search; every accepted candidate has an official success record.

The inner deadline is cooperative. A separate subprocess supervisor is required
for a hard end-to-end wallclock experiment (including candidate generation).
"""
from collections import Counter
import gzip
import json
import math
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent), str(HERE.parent / "solver"), str(HERE.parent / "探索")]
from common import DATA, OFFICIAL, atomic_json, digest, object_digest, read_json, single_active_plan
from graph_ir import GraphIR
from plan import validate_plan
from evaluator import evaluate
from advanced_solve import check_fixed_config
from operation_assign import generate_operation_candidates


def result_key(record):
    return record["metrics"]["makespan"], record["metrics"].get("data_movement_bytes", {}).get("added_copy_bytes", 0)


def load_official(record):
    if record.get("status") != "success" or digest(record["result_path"]) != record["result_sha256"]:
        raise ValueError("Missing or changed successful official result")
    with gzip.open(record["result_path"], "rt", encoding="utf-8") as stream:
        result = json.load(stream)
    if result["makespan"] != record["metrics"]["makespan"]:
        raise ValueError("Cached metrics differ from official result")
    return result


def source_hashes():
    paths = [HERE / name for name in ("__init__.py", "engine.py", "solve.py", "supervisor.py",
             "operation_assign.py", "component_baseline.py", "trace_refine.py", "cache_refine.py")]
    paths += list((HERE.parent / "solver").glob("*.py")) + list(OFFICIAL.glob("*.py"))
    paths += [HERE.parent / "探索" / name for name in ("advanced_solve.py", "operation_heft_probe.py", "partition_candidates.py")]
    return {str(p.resolve()): digest(p) for p in sorted(paths)}


def loose_lower_bound(ir, cores):
    """Necessary original compute/IO only; not a predicted achievable optimum."""
    from partition_candidates import topological_order
    order = topological_order(ir, "stable_id")
    finish = {}
    for op in order:
        finish[op] = max(0, ir.ops[op]["cycles"]) + max((finish[p] for p in ir.predecessors[op]), default=0)
    input_bytes = output_bytes = 0
    for e in ir.graph["edges"]:
        a, b = e["source"], e["target"]
        if a in ir.ops and ir.ops[a]["op"] == "COPY_IN" and b in ir.tensors:
            input_bytes += ir.tensors[b]["size"]
        if a in ir.tensors and b in ir.ops and ir.ops[b]["op"] == "COPY_OUT":
            output_bytes += ir.tensors[a]["size"]
    terms = {"M": ir.total_work_m / cores, "V": ir.total_work_v / cores,
             "compute_path": max(finish.values(), default=0), "original_DDR": (input_bytes + output_bytes) / 60.0}
    return {"value": max(terms.values(), default=0), "terms": terms,
            "scope": "necessary original work only; ignores sync, spill and contention timing"}


def generate_coarse_p1_candidates(ir, num_cores, max_candidates, seed):
    """P1 communicates between Tasks: use coarse, convex topology blocks."""
    from partition_candidates import topological_order, contiguous_blocks, block_views
    values, seen = [], set()
    for ordering in ("critical_path", "stable_id"):
        order = topological_order(ir, ordering)
        for target in (num_cores, 2 * num_cores, 4 * num_cores):
            blocks = contiguous_blocks(ir, order, target)
            view = block_views(ir, blocks)
            for method in ("p1_eft", "p1_balanced"):
                assignment, ends = [], []
                available = [0.0] * num_cores
                tasks = [0] * num_cores
                for bid, block in enumerate(blocks):
                    # Each P1 block is a Task. Root reads and inter-Task data
                    # remain DDR traffic even when adjacent Tasks share a core.
                    io = sum(ir.input_sizes[t] for t in view["root_inputs"][bid])
                    io += sum(2 * size for size, _ in view["incoming"][bid].values())
                    duration = view["work"][bid][2] + io / 60.0
                    def finish_on(core):
                        release = max((ends[p] + (100 if assignment[p] == core else 1000)
                                       for p in view["preds"][bid]), default=0)
                        return max(available[core] + (100 if tasks[core] else 0), release) + duration
                    if method == "p1_eft":
                        core = min(range(num_cores), key=lambda c: (finish_on(c), tasks[c], c))
                    else:
                        core = min(range(num_cores), key=lambda c: (available[c], finish_on(c), c))
                    end = finish_on(core)
                    assignment.append(core)
                    ends.append(end)
                    available[core], tasks[core] = end, tasks[core] + 1
                schedules = [[] for _ in range(num_cores)]
                for bid, core in enumerate(assignment):
                    schedules[core].append(bid)
                plan = {"node_to_subgraph": {str(op): bid for bid, block in enumerate(blocks) for op in block},
                        "core_schedules": schedules}
                validate_plan(ir, plan)
                h = object_digest(plan)
                if h not in seen:
                    seen.add(h)
                    values.append({"name": f"{ordering}_b{target}_{method}", "plan": plan,
                                   "metadata": {"family": "coarse_p1", "ordering": ordering,
                                                "blocks": len(blocks), "assignment": method,
                                                "proxy_end": max(ends, default=0)}})
    return values[:max_candidates], {"family": "coarse_p1", "unique_generated": len(values),
            "omitted": [c["name"] for c in values[max_candidates:]],
            "scope": "coarse contiguous topology blocks with P1 wait-cost proxy; no operation-per-Task assumption"}


class Search:
    def __init__(self, graph_path, cores, problem, run_dir, *, profile="full", seed=17,
                 budgets=None, max_rounds=3, timeout=None, total_budget_seconds=None,
                 total_evaluations=None, incumbent_plan=None, config_path=None,
                 evaluation_dir=None):
        if problem not in (1, 2, 3) or type(cores) is not int or not 1 <= cores <= 5:
            raise ValueError("problem 1..3 and cores 1..5 required")
        if profile not in ("component", "operation", "trace", "cache", "full"):
            raise ValueError("unknown profile")
        if profile == "cache" and problem != 3:
            raise ValueError("cache profile requires P3")
        if type(max_rounds) is not int or max_rounds < 1:
            raise ValueError("round count must be positive")
        self.started = time.perf_counter()
        self.graph_path = Path(graph_path).resolve()
        self.config_path = Path(config_path or self.graph_path.parent / "config.txt").resolve()
        check_fixed_config(self.config_path)
        self.run_dir = Path(run_dir).resolve()
        if self.run_dir == DATA.parent or DATA.parent in self.run_dir.parents:
            raise ValueError("artifacts must be outside official attachments")
        if self.run_dir.exists() and any(self.run_dir.iterdir()):
            raise ValueError("run directory must be empty; use a fresh attempt and preserve earlier evidence")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.ir = GraphIR.from_path(self.graph_path)
        self.cores, self.problem, self.profile, self.seed = cores, problem, profile, seed
        self.budgets = dict({"component": 6, "operation": 12, "trace": 24, "cache": 24} if budgets is None else budgets)
        if set(self.budgets) - {"component", "operation", "trace", "cache"}:
            raise ValueError("unknown stage cap")
        if any(type(n) is not int or n < 0 for n in self.budgets.values()):
            raise ValueError("stage evaluation caps must be nonnegative integers")
        self.max_rounds = max_rounds
        self.timeout = timeout if timeout is not None else (60 if len(self.ir.compute_ids) <= 10000 else 180)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if total_budget_seconds is not None and (not math.isfinite(total_budget_seconds) or total_budget_seconds <= 0):
            raise ValueError("total budget must be positive")
        if total_evaluations is not None and (type(total_evaluations) is not int or total_evaluations < 1):
            raise ValueError("total evaluation cap must be positive")
        self.deadline = None if total_budget_seconds is None else self.started + total_budget_seconds
        self.total_evaluations = total_evaluations
        self.evaluation_dir = Path(evaluation_dir).resolve() if evaluation_dir else self.run_dir / "evaluations"
        if self.evaluation_dir == DATA.parent or DATA.parent in self.evaluation_dir.parents:
            raise ValueError("evaluation outputs must be outside official attachments")
        self.incumbent_plan = incumbent_plan
        self.best = None
        self.evaluations, self.stages, self.failures, self.duplicates = [], [], [], []
        self.seen = set()
        self.lower_bound = loose_lower_bound(self.ir, cores)
        self.manifest = {"graph_path": str(self.graph_path), "graph_sha256": digest(self.graph_path),
                         "config_path": str(self.config_path), "config_sha256": digest(self.config_path),
                         "case": self.graph_path.stem, "problem": problem, "num_cores": cores,
                         "profile": profile, "seed": seed, "budgets": self.budgets,
                         "max_rounds": max_rounds, "per_evaluation_timeout": self.timeout,
                         "total_budget_seconds": total_budget_seconds, "total_evaluations": total_evaluations,
                         "budget_semantics": "inner cooperative deadline; use supervisor.py for hard whole-process deadline",
                         "source_sha256": source_hashes(), "python": sys.version,
                         "initial_plan_sha256": object_digest(incumbent_plan) if incumbent_plan else None,
                         "selection": "official success; makespan then added_copy_bytes",
                         "lower_bound": self.lower_bound}
        atomic_json(self.run_dir / "manifest.json", self.manifest)
        self.checkpoint("initialized")

    def available(self):
        return ((self.total_evaluations is None or len(self.evaluations) < self.total_evaluations)
                and (self.deadline is None or time.perf_counter() < self.deadline - .1))

    def checkpoint(self, state):
        out = {**self.manifest, "state": state, "status": "success" if self.best else "no_feasible_result",
               "best": self.best, "evaluations": self.evaluations, "stages": self.stages,
               "generation_failures": self.failures, "duplicates": self.duplicates,
               "evaluated_count": len(self.evaluations), "status_counts": dict(Counter(r["record"]["status"] for r in self.evaluations)),
               "official_calls": sum(not r["record"].get("cache_hit", False) for r in self.evaluations),
               "cache_hits": sum(bool(r["record"].get("cache_hit", False)) for r in self.evaluations),
               "elapsed_seconds": time.perf_counter() - self.started}
        atomic_json(self.run_dir / "checkpoint.json", out)
        if self.best:
            atomic_json(self.run_dir / "best.plan.json", self.best["plan"])
        return out

    def trial(self, candidate, stage):
        validate_plan(self.ir, candidate["plan"])
        if len(candidate["plan"]["core_schedules"]) != self.cores:
            raise ValueError("candidate core-count mismatch")
        hashed = object_digest(candidate["plan"])
        if hashed in self.seen:
            self.duplicates.append({"stage": stage, "name": candidate["name"], "plan_sha256": hashed})
            return False
        if not self.available():
            return False
        self.seen.add(hashed)
        limit = self.timeout if self.deadline is None else min(self.timeout, self.deadline - time.perf_counter())
        record = evaluate(self.graph_path, candidate["plan"], self.problem, self.evaluation_dir,
                          timeout=max(.01, limit), config_path=self.config_path)
        row = {"stage": stage, "name": candidate["name"], "plan_sha256": hashed,
               "metadata": candidate.get("metadata", {}), "record": record}
        self.evaluations.append(row)
        if record["status"] == "success" and (self.best is None or result_key(record) < result_key(self.best["record"])):
            self.best = {**row, "plan": candidate["plan"]}
        self.checkpoint("searching")
        return True

    def stage(self, name, generator, budget, iterative=False):
        used = 0
        for round_index in range(self.max_rounds if iterative else 1):
            if not self.available() or used >= budget or (iterative and self.best is None):
                break
            before = result_key(self.best["record"]) if self.best else None
            start = time.perf_counter()
            try:
                candidates, diagnostics = generator(round_index, min(12, budget - used) if iterative else budget)
            except Exception as e:
                import traceback
                self.failures.append({"stage": name, "round": round_index, "error": traceback.format_exc()})
                self.checkpoint("generation_error")
                break
            entry = {"stage": name, "round": round_index, "before": before,
                     "generation_seconds": time.perf_counter() - start, "diagnostics": diagnostics,
                     "candidate_names": [c["name"] for c in candidates], "evaluation_start": len(self.evaluations)}
            for candidate in candidates:
                if used >= budget or not self.available():
                    break
                try:
                    used += int(self.trial(candidate, name))
                except (ValueError, TypeError, KeyError) as e:
                    self.failures.append({"stage": name, "round": round_index, "candidate": candidate["name"], "error": str(e)})
            entry.update({"after": result_key(self.best["record"]) if self.best else None,
                          "evaluation_end": len(self.evaluations)})
            self.stages.append(entry)
            self.checkpoint("stage_complete")
            # A changed trace is needed for the next adaptive round. A tie in
            # makespan with lower bytes is still a legitimate new incumbent.
            if iterative and (self.best is None or result_key(self.best["record"]) == before):
                break

    def run(self):
        from component_baseline import generate_component_candidates
        if self.incumbent_plan is not None:
            self.trial({"name": "provided_incumbent", "plan": self.incumbent_plan, "metadata": {"family": "common_initial_plan"}}, "initial")
        self.stage("component", lambda r, n: generate_component_candidates(self.ir, self.cores, max_candidates=max(1, n), seed=self.seed), self.budgets.get("component", 0))
        if self.best is None and self.available():
            self.trial({"name": "single_active_fallback", "plan": single_active_plan(self.ir.graph, self.cores)}, "fallback")
        if self.profile != "component":
            operation_generator = generate_coarse_p1_candidates if self.problem == 1 else generate_operation_candidates
            self.stage("operation", lambda r, n: operation_generator(self.ir, self.cores, max_candidates=max(1, n), seed=self.seed), self.budgets.get("operation", 0))
        if self.problem in (2, 3) and self.profile in ("trace", "cache", "full"):
            from trace_refine import generate_trace_candidates
            self.stage("trace", lambda r, n: generate_trace_candidates(self.ir, self.best["plan"], load_official(self.best["record"]),
                       num_cores=self.cores, max_candidates=max(1, n), round_index=r, seed=self.seed), self.budgets.get("trace", 0), iterative=True)
        if self.problem == 3 and self.profile in ("cache", "full"):
            from cache_refine import generate_cache_candidates
            self.stage("cache", lambda r, n: generate_cache_candidates(self.ir, self.best["plan"], load_official(self.best["record"]),
                       num_cores=self.cores, max_candidates=max(1, n), round_index=r, seed=self.seed), self.budgets.get("cache", 0), iterative=True)
        result = self.checkpoint("finished")
        if source_hashes() != self.manifest["source_sha256"]:
            raise RuntimeError("Source changed during search; result kept in checkpoint but run is not reproducible")
        if digest(self.graph_path) != self.manifest["graph_sha256"] or digest(self.config_path) != self.manifest["config_sha256"]:
            raise RuntimeError("Official input changed during search")
        result["completed"] = True
        result["stop_reason"] = "budget" if not self.available() else "stages_exhausted_or_stagnated"
        atomic_json(self.run_dir / "summary.json", result)
        return result
