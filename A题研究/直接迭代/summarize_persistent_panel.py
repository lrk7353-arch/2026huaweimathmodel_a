"""Audit paid cold-search panel results and export portable evidence, not just winners."""
import argparse
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
from statistics import mean
import tarfile

from common_run import atomic_json, read_json, score, write_csv
from run_persistent_panel import ROOT, VARIANTS, calls_of, completed_attempt, sha256, validate_protocol


def record_key(record):
    return record.get("attempt_id") or record.get("record_path") or json.dumps(record, sort_keys=True)


def initialization(summary):
    calls = calls_of(summary)
    count = summary.get("initialization_calls")
    if isinstance(count, list):
        initial = count
    elif isinstance(count, int):
        initial = calls[:count]
    else:
        initial = [c for c in calls if c.get("phase", c.get("stage")) in ("init", "initialization")]
    hashes = summary.get("initialization_plan_hashes")
    if hashes is None:
        hashes = [c.get("record", {}).get("hashes", {}).get("plan_sha256") for c in initial]
    successes = [c["record"]["hashes"]["plan_sha256"] for c in initial
                 if c.get("record", {}).get("status") == "success"]
    return dict(plan_hashes=hashes, success_hashes=successes, calls=len(initial))


def generation_time(summary, directory):
    total = summary.get("generation_seconds", 0.)
    # The mature wrapper reports tail generation separately from prefix stages.
    # This is logged generation time, not a claim to observe all internal work.
    prefix_path = directory / "mature_prefix/summary.json"
    if prefix_path.exists():
        prefix = read_json(prefix_path)
        total += sum(s.get("generation_seconds", 0.) for s in prefix.get("stages", []))
    return total


def branch_rows(summary, identity):
    rows = []
    for i, event in enumerate(summary.get("branch_events", [])):
        rows.append(dict(identity, event_index=i + 1, **event))
    return rows


def all_attempt_records(arm_dir):
    """Count aborted and recovered evaluation attempts once, with missing-data flags."""
    records, unresolved = {}, []
    for directory in sorted(p for p in arm_dir.glob("attempt_*") if p.is_dir()):
        paths = [directory / "summary.json", directory / "progress.json"]
        snapshot = next((read_json(p) for p in paths if p.exists()), {})
        for call in calls_of(snapshot):
            if "record" in call:
                record = call["record"]
                records[record_key(record)] = record
        for path in directory.rglob("record.json"):
            record = read_json(path)
            if "status" in record and "problem" in record and "metrics" in record:
                records[record_key(record)] = record
        for path in directory.rglob("request.json"):
            if not (path.parent / "record.json").exists():
                unresolved.append(str(path.relative_to(arm_dir)))
    return list(records.values()), unresolved


def baseline_map(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    result = {}
    for row in rows:
        c = row["case"]
        value = float(row["original_singlecore"])
        if c in result and result[c] != value:
            raise ValueError("Conflicting official original single-core baseline")
        result[c] = value
    return result


def archive_plan(record, plans):
    path = record.get("plan_path")
    if not path:
        return None
    if not Path(path).is_file() and record.get("status") != "success":
        return None
    data = Path(path).read_bytes()
    h = hashlib.sha256(data).hexdigest()
    expected = record.get("hashes", {}).get("plan_sha256")
    if expected and h != expected:
        raise ValueError("Plan hash changed: " + path)
    plans[h] = data
    return "plans/" + h + ".json"


def portable_calls(summary, plans):
    result = []
    for index, call in enumerate(calls_of(summary)):
        copied = dict(call)
        copied["paid_call_index"] = index + 1
        record = call.get("record", {})
        copied["archived_plan"] = archive_plan(record, plans)
        result.append(copied)
    return result


def summarize(source, output, allow_partial=False):
    source, output = source.resolve(), output.resolve()
    protocol = read_json(source / "protocol.json")
    validate_protocol(protocol)
    output.mkdir(parents=True, exist_ok=True)
    baseline_path = ROOT / "真机启发攻坚_20260926/从头完整1500/从头1500配置成绩.csv"
    baselines = baseline_map(baseline_path)
    rows, tradeoffs, prefixes, branches, archives, plans = [], [], [], [], [], {}
    initialization_checks, missing, incomplete_records = [], [], []
    for config in protocol["configurations"]:
        identity = dict(config_id=config["id"], case=f"case_{config['case']:03d}",
                        problem=config["problem"], cores=config["cores"], group=config["group"])
        init = {}
        for variant in protocol["variants"]:
            arm_dir = source / "configurations" / config["id"] / variant
            done = completed_attempt(arm_dir, protocol["budget"])
            attempts = sorted(p for p in arm_dir.glob("attempt_*") if p.is_dir())
            if not done:
                pending, unresolved = all_attempt_records(arm_dir)
                missing.append(dict(identity, variant=variant, started_attempts=len(attempts),
                                    recorded_calls=len(pending),
                                    recorded_failures=sum(r.get("status") != "success" for r in pending),
                                    unresolved_evaluation_attempts=len(unresolved)))
                for record in pending:
                    archive_plan(record, plans)
                incomplete_records.append(dict(identity, variant=variant, records=pending,
                                               unresolved_evaluation_attempts=unresolved))
                continue
            path, summary = done
            calls = calls_of(summary)
            paid = [c["record"] for c in calls if c.get("record", {}).get("status") == "success"]
            best = summary.get("best_record")
            all_records, unresolved = all_attempt_records(arm_dir)
            first_path = attempts[0] / "summary.json" if attempts else None
            first = read_json(first_path) if first_path and first_path.exists() else {}
            row = dict(identity, variant=variant, completed=True, valid=bool(best),
                       attempt_count=len(attempts), recovered=len(attempts) > 1,
                       first_attempt_valid=bool(first.get("best_record")) and bool(first.get("complete")),
                       calls=len(calls), failures=len(calls) - len(paid),
                       cache_hits=sum(c.get("record", {}).get("cache_hit", False) for c in calls),
                       seconds=summary.get("elapsed_seconds"),
                       deadline_overrun_seconds=max(0., summary.get("elapsed_seconds", 0.) - protocol["seconds"]),
                       logged_generation_seconds=generation_time(summary, path.parent),
                       generation_errors=len(summary.get("generation_errors", [])),
                       total_recorded_calls_including_recovery=len(all_records),
                       total_recorded_failures_including_recovery=sum(r.get("status") != "success" for r in all_records),
                       recorded_evaluation_seconds_including_recovery=sum(r.get("elapsed_seconds", 0.) for r in all_records),
                       unresolved_evaluation_attempts=len(unresolved),
                       summary=str(path.relative_to(source)),
                       original_singlecore=baselines[identity["case"]])
            if best:
                t, copy = score(best)
                cache = best["metrics"].get("cache_stats", {}) or {}
                hit, miss = cache.get("hit_bytes", 0), cache.get("miss_bytes", 0)
                row.update(makespan=t, added_copy=copy, raw_speedup=baselines[identity["case"]] / t,
                           statement_speedup=1. if config["cores"] == 1 else baselines[identity["case"]] / t,
                           hit_bytes=hit, miss_bytes=miss,
                           byte_hit_rate=hit / max(1, hit + miss) if config["problem"] == 3 else None,
                           plan_sha256=best["hashes"]["plan_sha256"])
                for allowance in (0., .01, .03):
                    eligible = [r for r in paid if score(r)[0] <= t * (1 + allowance)]
                    chosen = min(eligible, key=lambda r: (score(r)[1], score(r)[0]))
                    tradeoffs.append(dict(identity, variant=variant, allowed_time_regression=allowance,
                                          makespan=score(chosen)[0], added_copy=score(chosen)[1],
                                          actual_time_regression=score(chosen)[0] / t - 1,
                                          bytes_saved=copy - score(chosen)[1],
                                          archived_plan=archive_plan(chosen, plans)))
            for cap in (8, 12, 16, 24):
                pool = [c["record"] for c in calls[:cap] if c.get("record", {}).get("status") == "success"]
                chosen = min(pool, key=score) if pool else None
                prefixes.append(dict(identity, variant=variant, prefix_budget=cap,
                                     paid_calls=min(len(calls), cap),
                                     makespan=score(chosen)[0] if chosen else None,
                                     added_copy=score(chosen)[1] if chosen else None,
                                     interpretation="prefix of this B24 run; not a separate budget experiment"))
            init[variant] = initialization(summary)
            arm_identity = dict(identity, variant=variant)
            events = branch_rows(summary, arm_identity)
            branches.extend(events)
            depths = [x.get("depth", x.get("parent_depth", x.get("accepted_depth", 0))) for x in calls]
            event_depths = [x.get("depth", x.get("accepted_depth", 0)) for x in events]
            row["max_reported_depth"] = max([0] + [v for v in depths + event_depths if isinstance(v, (int, float))])
            branch_ids = {str(c.get("lineage", c.get("branch_id", c.get("branch")))) for c in calls
                          if c.get("lineage", c.get("branch_id", c.get("branch"))) is not None}
            row["evaluated_branch_count"] = len(branch_ids)
            rows.append(row)
            archive = dict(arm_identity, summary=dict(summary, calls=portable_calls(summary, plans)),
                           source_summary=str(path.relative_to(source)), initialization=init[variant],
                           recovery_records=all_records, unresolved_evaluation_attempts=unresolved)
            for record in all_records:
                if record.get("plan_path"):
                    archive_plan(record, plans)
            archives.append(archive)
        if "legacy" in init and "persistent" in init:
            initialization_checks.append(dict(identity,
                hashes_equal=init["legacy"]["plan_hashes"] == init["persistent"]["plan_hashes"],
                successful_hashes_equal=init["legacy"]["success_hashes"] == init["persistent"]["success_hashes"],
                legacy=init["legacy"], persistent=init["persistent"]))
    if missing and not allow_partial:
        raise ValueError(f"{len(missing)} frozen arms lack complete summaries; use --allow-partial for development snapshots")
    index = {(r["config_id"], r["variant"]): r for r in rows}
    paired, aggregates = [], []
    for config in protocol["configurations"]:
        for baseline in ("legacy", "mature"):
            old, new = index.get((config["id"], baseline)), index.get((config["id"], "persistent"))
            if not old or not new or not old["valid"] or not new["valid"]:
                continue
            diff = new["makespan"] - old["makespan"]
            paired.append(dict(config_id=config["id"], case=new["case"], problem=new["problem"],
                               cores=new["cores"], group=new["group"], baseline=baseline,
                               baseline_makespan=old["makespan"], persistent_makespan=new["makespan"],
                               baseline_speedup=old["statement_speedup"], persistent_speedup=new["statement_speedup"],
                               outcome="win" if diff < 0 else "loss" if diff > 0 else "tie",
                               time_reduction=1 - new["makespan"] / old["makespan"],
                               copy_delta=new["added_copy"] - old["added_copy"],
                               baseline_calls=old["calls"], persistent_calls=new["calls"],
                               baseline_seconds=old["seconds"], persistent_seconds=new["seconds"]))
    groups = ["all"] + sorted({c["group"] for c in protocol["configurations"]})
    for p in (1, 2, 3):
        for n in range(1, 6):
            for group in groups:
                for baseline in ("legacy", "mature"):
                    subset = [r for r in paired if r["problem"] == p and r["cores"] == n
                              and r["baseline"] == baseline and (group == "all" or r["group"] == group)]
                    if not subset:
                        continue
                    aggregates.append(dict(problem=p, cores=n, group=group, baseline=baseline, count=len(subset),
                        baseline_mean_speedup=mean(r["baseline_speedup"] for r in subset),
                        persistent_mean_speedup=mean(r["persistent_speedup"] for r in subset),
                        wins=sum(r["outcome"] == "win" for r in subset),
                        ties=sum(r["outcome"] == "tie" for r in subset),
                        losses=sum(r["outcome"] == "loss" for r in subset),
                        copy_increase_cases=sum(r["copy_delta"] > 0 for r in subset),
                        time_tie_copy_regressions=sum(r["outcome"] == "tie" and r["copy_delta"] > 0 for r in subset)))
    curves = []
    for variant in protocol["variants"]:
        for p in (1, 2, 3):
            for n in range(1, 6):
                subset = [r for r in rows if r["variant"] == variant and r["problem"] == p
                          and r["cores"] == n and r["valid"]]
                if subset:
                    curves.append(dict(variant=variant, problem=p, cores=n, count=len(subset),
                        statement_mean_speedup=mean(r["statement_speedup"] for r in subset),
                        raw_optimized_mean_ratio=mean(r["raw_speedup"] for r in subset),
                        graph_set=";".join(sorted(r["case"] for r in subset)),
                        interpretation="panel only; graph sets may differ by core count; not full-pool curve"))
    cache_comparisons = []
    for r in rows:
        if r["problem"] != 3 or not r["valid"]:
            continue
        other_id = r["config_id"].replace("_p3_", "_p2_")
        other = index.get((other_id, r["variant"]))
        if other and other["valid"]:
            cache_comparisons.append(dict(case=r["case"], cores=r["cores"], variant=r["variant"],
                p2_makespan=other["makespan"], p3_makespan=r["makespan"],
                same_core_p2_over_p3=other["makespan"] / r["makespan"],
                identical_plan_hash=other["plan_sha256"] == r["plan_sha256"],
                interpretation="separately optimized plans; includes scheduling differences"))
    for name, values in [("per_arm.csv", rows), ("paired_comparison.csv", paired),
                         ("paid_copy_tradeoffs.csv", tradeoffs), ("paid_budget_prefixes.csv", prefixes),
                         ("branch_events.csv", branches), ("same_core_p2_p3.csv", cache_comparisons),
                         ("statement_curves.csv", curves),
                         ("aggregate.csv", aggregates)]:
        write_csv(output / name, values)
    summary = dict(expected_configurations=len(protocol["configurations"]),
                   expected_arms=len(protocol["configurations"]) * len(protocol["variants"]),
                   completed_arms=len(rows), valid_arms=sum(r["valid"] for r in rows), missing_arms=missing,
                   first_attempt_valid_arms=sum(r["first_attempt_valid"] for r in rows),
                   calls=sum(r["calls"] for r in rows), failures=sum(r["failures"] for r in rows),
                   total_recorded_calls_including_recovery=sum(r["total_recorded_calls_including_recovery"] for r in rows),
                   recorded_calls_in_incomplete_arms=sum(r["recorded_calls"] for r in missing),
                   all_recorded_calls=sum(r["total_recorded_calls_including_recovery"] for r in rows)
                                      + sum(r["recorded_calls"] for r in missing),
                   initialization_mismatches=sum(not r["hashes_equal"] or not r["successful_hashes_equal"] for r in initialization_checks),
                   initialization_comparisons=len(initialization_checks), aggregates=aggregates,
                   notes=["No historical plans used as free initial states", "Frozen regression, not blind evaluation",
                          "Means compare paired successful results; missing/failed arms reported separately",
                          "Generation time covers explicitly logged stages; full elapsed time includes unlogged work",
                          "Single-core statement speedup is 1; raw optimized ratio remains available",
                          "B8/B12/B16 are paid prefixes, not independent reruns"])
    atomic_json(output / "summary.json", summary)
    atomic_json(output / "initialization_checks.json", initialization_checks)
    atomic_json(output / "protocol.json", protocol)
    manifest_path = source / "execution_manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else None
    atomic_json(output / "evidence_sources.json", dict(execution_manifest=manifest,
        baseline_ledger=str(baseline_path.relative_to(ROOT)), baseline_sha256=sha256(baseline_path),
        archived_plan_count=len(plans)))
    with gzip.open(output / "all_paid_calls_and_recovery.json.gz", "wt", encoding="utf-8") as stream:
        json.dump(archives, stream, ensure_ascii=False, separators=(",", ":"))
    with gzip.open(output / "incomplete_arm_records.json.gz", "wt", encoding="utf-8") as stream:
        json.dump(incomplete_records, stream, ensure_ascii=False, separators=(",", ":"))
    with tarfile.open(output / "all_evaluated_plans.tar.gz", "w:gz") as archive:
        for h, data in sorted(plans.items()):
            info = tarfile.TarInfo("plans/" + h + ".json")
            info.size = len(data)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    result = summarize(args.source, args.out, args.allow_partial)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
