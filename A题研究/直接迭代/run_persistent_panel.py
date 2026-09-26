"""Frozen cold-start panel; independent arms, balanced order, explicit recovery cost.

Freeze first, then run without changing code or protocol.  A completed summary is
reused only after checking that its selected plan was paid for in that arm.  An
interrupted attempt is preserved and a fresh attempt is started; its spent calls
are recovery cost, never a free initial state.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import itertools
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import traceback

from common_run import DATA, atomic_json, read_json, score

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
VARIANTS = ("legacy", "persistent", "mature")
P1_DEVELOPMENT = (3, 16, 43, 54, 58, 62, 75, 85, 87)
P23_DEVELOPMENT = (19, 35, 49, 50, 66)
P23_REGRESSION = (1, 9, 23, 25, 28, 37, 46, 53, 71, 95)
SALT = "persistent-joint-sprint-20260926-v1"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stable_key(value, purpose=""):
    return hashlib.sha256(f"{SALT}/{purpose}/{value}".encode()).hexdigest()


def config_id(case, problem, cores):
    return f"case_{int(case):03d}_p{int(problem)}_n{int(cores)}"


def frozen_protocol():
    """Selection uses identifiers only, never observed timing or selected plans."""
    used = set(P1_DEVELOPMENT + P23_DEVELOPMENT + P23_REGRESSION)
    eligible = [n for n in range(1, 101) if n not in used]
    pool = sorted(eligible, key=lambda n: stable_key(n, "pool"))[:20]
    regression = sorted(pool, key=lambda n: stable_key(n, "regression"))[:4]
    configs = []

    def add(case, problem, cores, group):
        configs.append(dict(id=config_id(case, problem, cores), case=case,
                            problem=problem, cores=cores, group=group))

    for c in P1_DEVELOPMENT:
        add(c, 1, 5, "development")
    for p in (2, 3):
        for c in P23_DEVELOPMENT:
            add(c, p, 5, "development")
        for c in P23_REGRESSION:
            add(c, p, 5, "existing_regression")
    for p in (1, 2, 3):
        for c in regression:
            add(c, p, 5, "frozen_regression")
    # The same two identifier-selected graphs at every low core count make the
    # small core curve coherent.  They are not independent unseen graph tests.
    low_core_cases = sorted(regression, key=lambda n: stable_key(n, "cores"))[:2]
    for p in (1, 2, 3):
        for n in range(1, 5):
            for c in low_core_cases:
                add(c, p, n, "core_regression")
    assert len(configs) == len({c["id"] for c in configs}) == 75
    permutations = list(itertools.permutations(VARIANTS))
    # Balance within each scenario/core stratum; a deterministic offset keeps
    # no scenario tied to the same first arm.  Filtering variants later retains
    # the frozen relative order, and is recorded in execution sessions.
    for p in (1, 2, 3):
        for n in range(1, 6):
            group = sorted((c for c in configs if c["problem"] == p and c["cores"] == n),
                           key=lambda c: stable_key(c["id"], "within-stratum"))
            offset = int(stable_key(f"{p}/{n}", "order-offset")[:8], 16) % 6
            for i, c in enumerate(group):
                c["arm_order"] = list(permutations[(i + offset) % 6])
    configs.sort(key=lambda c: stable_key(c["id"], "configuration-order"))
    return dict(schema_version=1, protocol_name=SALT, configurations=configs,
                variants=list(VARIANTS), budget=24, seconds=240., timeout=60., workers=2,
                regression_pool=pool, frozen_regression_cases=regression,
                low_core_cases=low_core_cases,
                selection="identifier SHA256 only; all graphs historically used; not blind",
                comparison="same initialization legacy loop vs persistent; mature release separate",
                initialization_check="compare paid initialization hashes and success sets; disclose any mismatch",
                source_policy="one frozen source manifest for all runs; abort on mutation",
                recovery_policy="preserve interrupted attempts; restart cold, disclose all spent calls",
                mature_policy="P1 mature; P2/P3 frontier; legacy operation policy; no time extension",
                single_core_curve="statement speedup point is 1; raw optimized ratio reported separately")


def validate_protocol(protocol):
    assert protocol["budget"] > 0 and protocol["seconds"] > 0 and protocol["timeout"] > 0
    assert protocol["workers"] > 0
    configs = protocol["configurations"]
    assert len({c["id"] for c in configs}) == len(configs)
    for c in configs:
        assert c["id"] == config_id(c["case"], c["problem"], c["cores"])
        assert c["problem"] in (1, 2, 3) and c["cores"] in range(1, 6)
        assert 1 <= c["case"] <= 100
        assert sorted(c["arm_order"]) == sorted(protocol["variants"])


def source_manifest():
    """Include imported research modules, solver wrappers and official sources."""
    files = set(ROOT.glob("*.py"))
    files.update((REPO / "A题研究").glob("*.py"))
    for base in (REPO / "A题研究/solver", REPO / "A题研究/精修求解器",
                 REPO / "A题研究/advanced_solver",
                 REPO / "选题分析/A题附件/code"):
        files.update(base.rglob("*.py"))
    return {str(p.relative_to(REPO)): sha256(p) for p in sorted(files)}


def assert_sources(expected):
    actual = source_manifest()
    if actual != expected:
        changed = sorted(k for k in set(actual) | set(expected) if actual.get(k) != expected.get(k))
        raise RuntimeError("Frozen source changed: " + ", ".join(changed))


def calls_of(summary):
    return summary.get("calls", summary.get("evaluations", []))


def validate_complete(summary, budget):
    if not summary.get("complete", summary.get("completed", False)):
        return False
    calls = calls_of(summary)
    if len(calls) != summary.get("logical_calls", len(calls)) or len(calls) > budget:
        raise ValueError("Inconsistent paid call accounting")
    paid = [c["record"] for c in calls if c.get("record", {}).get("status") == "success"]
    best = summary.get("best_record")
    if paid:
        if not best or score(best) != min(map(score, paid)):
            raise ValueError("Best result is not the best paid candidate")
        hashes = {r.get("hashes", {}).get("plan_sha256") for r in paid if score(r) == score(best)}
        if best.get("hashes", {}).get("plan_sha256") not in hashes:
            raise ValueError("Selected plan was not paid for")
        path = Path(best["plan_path"])
        if not path.is_file() or sha256(path) != best["hashes"]["plan_sha256"]:
            raise ValueError("Selected plan missing or changed")
    elif best:
        raise ValueError("Best result exists without successful paid call")
    return True


def completed_attempt(arm_dir, budget):
    for path in sorted(arm_dir.glob("attempt_*/summary.json")):
        summary = read_json(path)
        if validate_complete(summary, budget):
            return path, summary
    return None


def execute_pair(job):
    config, root, protocol, manifest, variants = job
    pair_dir = Path(root) / "configurations" / config["id"]
    pair_dir.mkdir(parents=True, exist_ok=True)
    arms = {}
    for variant in config["arm_order"]:
        if variant not in variants:
            continue
        arm_dir = pair_dir / variant
        arm_dir.mkdir(exist_ok=True)
        done = completed_attempt(arm_dir, protocol["budget"])
        if done:
            path, summary = done
            arms[variant] = dict(summary=str(path.relative_to(root)), reused_complete=True,
                                 valid_result=bool(summary.get("best_record")))
            continue
        assert_sources(manifest)
        previous = sorted(p for p in arm_dir.glob("attempt_*") if p.is_dir())
        numbers = [int(p.name.split("_")[-1]) for p in previous]
        attempt = arm_dir / f"attempt_{max(numbers, default=0) + 1:03d}"
        # run() owns creation of this directory.  Sidecar records preserve the
        # attempt identity even if run() fails before creating its output path.
        start_record = dict(config=config, variant=variant, source_manifest_sha256=
                            hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
                            started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            prior_attempts=len(previous), cold_start=True)
        atomic_json(arm_dir / (attempt.name + ".start.json"), start_record)
        try:
            if variant == "mature":
                from frontier_solver import run
                summary = run(f"case_{config['case']:03d}", config["problem"], config["cores"],
                              attempt, protocol["budget"], protocol["seconds"], protocol["timeout"],
                              "mature" if config["problem"] == 1 else "frontier", "legacy")
            else:
                from persistent_search import run
                summary = run(f"case_{config['case']:03d}", config["problem"], config["cores"],
                              attempt, budget=protocol["budget"], seconds=protocol["seconds"],
                              timeout=protocol["timeout"], variant=variant)
            if not validate_complete(summary, protocol["budget"]):
                raise ValueError("run returned an incomplete summary")
            assert_sources(manifest)
            arms[variant] = dict(summary=str((attempt / "summary.json").relative_to(root)),
                                 reused_complete=False, valid_result=bool(summary.get("best_record")))
        except Exception as exc:
            attempt.mkdir(parents=True, exist_ok=True)
            atomic_json(attempt / "runner_error.json", dict(error=repr(exc), traceback=traceback.format_exc()))
            arms[variant] = dict(error=repr(exc), attempt=str(attempt.relative_to(root)), valid_result=False)
            if "Frozen source changed" in str(exc):
                raise
        atomic_json(pair_dir / "pair.json", dict(configuration=config, arms=arms))
    result = dict(configuration=config, arms=arms)
    atomic_json(pair_dir / "pair.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, help="existing protocol; if absent create frozen default")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--variants", nargs="+", choices=VARIANTS, default=list(VARIANTS))
    parser.add_argument("--config-ids", nargs="+", help="execute an explicitly logged subset of frozen configurations")
    parser.add_argument("--freeze-only", action="store_true")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.protocol:
        protocol = read_json(args.protocol)
    elif (out / "protocol.json").exists():
        protocol = read_json(out / "protocol.json")
    else:
        protocol = frozen_protocol()
    validate_protocol(protocol)
    if (out / "protocol.json").exists() and read_json(out / "protocol.json") != protocol:
        raise ValueError("Output directory already has a different frozen protocol")
    atomic_json(out / "protocol.json", protocol)
    if args.freeze_only:
        print(json.dumps(dict(protocol=str(out / "protocol.json"), configurations=len(protocol["configurations"]),
                              regression=protocol["frozen_regression_cases"], low_cores=protocol["low_core_cases"])))
        return
    if args.workers != protocol["workers"]:
        raise ValueError("Worker count must equal frozen protocol")
    manifest_path = out / "execution_manifest.json"
    resuming = manifest_path.exists()
    if resuming:
        manifest = read_json(manifest_path)
        if manifest["protocol_sha256"] != sha256(out / "protocol.json"):
            raise ValueError("Frozen protocol changed after execution began")
        if manifest["hostname"] != platform.node() or manifest["python"] != sys.version:
            raise ValueError("Frozen panel must resume on the same host and Python runtime")
        assert_sources(manifest["sources"])
        for name, expected in manifest["inputs"].items():
            if sha256(REPO / name) != expected:
                raise ValueError("Frozen input changed: " + name)
    else:
        inputs = {str((DATA / f"case_{c['case']:03d}.json").relative_to(REPO)):
                  sha256(DATA / f"case_{c['case']:03d}.json") for c in protocol["configurations"]}
        inputs[str((DATA / "config.txt").relative_to(REPO))] = sha256(DATA / "config.txt")
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                                  text=True, check=False).stdout.strip()
        manifest = dict(sources=source_manifest(), inputs=inputs, revision=revision,
                        python=sys.version, executable=sys.executable, platform=platform.platform(),
                        hostname=platform.node(), cpu_count=os.cpu_count(), workers=args.workers,
                        protocol_sha256=sha256(out / "protocol.json"))
        atomic_json(manifest_path, manifest)
    configs = protocol["configurations"]
    if args.config_ids:
        unknown = set(args.config_ids) - {c["id"] for c in configs}
        if unknown:
            raise ValueError("Unknown frozen configurations: " + repr(unknown))
        configs = [c for c in configs if c["id"] in args.config_ids]
    session = out / "sessions" / (time.strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}.json")
    atomic_json(session, dict(config_ids=[c["id"] for c in configs], variants=args.variants,
                              workers=args.workers, resumed=resuming))
    jobs = [(c, str(out), protocol, manifest["sources"], args.variants) for c in configs]
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(execute_pair, job): job[0]["id"] for job in jobs}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            atomic_json(out / "latest_session_results.json", results)
            print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
