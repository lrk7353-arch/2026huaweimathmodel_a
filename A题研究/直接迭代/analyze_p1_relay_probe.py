"""Read-only analysis of a warm P1 relay experiment; never runs an evaluator.

Only writes 分析汇总.json and 逐起点对照.csv in --out. Incomplete batches are
explicitly provisional. Historical seed acquisition and later independent
acceptance replays are outside the separately reported current-run costs.
"""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path


FAMILIES = ("legacy_joint", "region_joint")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def score(record):
    if not record or record.get("status") != "success":
        return None
    metrics = record.get("metrics", {})
    time = metrics.get("makespan")
    copy = metrics.get("data_movement_bytes", {}).get("added_copy_bytes")
    if time is None or copy is None:
        return None
    return time, copy


def relation(actual, reference):
    if actual is None:
        return "unavailable"
    return "win" if actual < reference else "loss" if actual > reference else "tie"


def reduction(reference, actual):
    return None if actual is None or reference <= 0 else 1 - actual / reference


def disk_cost(directory):
    attempts = sorted(p for p in directory.glob("**/attempts/*") if p.is_dir())
    records = {str(p / "record.json"): read(p / "record.json")
               for p in attempts if (p / "record.json").exists()}
    statuses = Counter(r.get("status", "unknown") for r in records.values())
    return records, dict(
        attempt_directories=len(attempts), official_recorded_calls=len(records),
        successes=statuses.get("success", 0),
        failures=sum(v for k, v in statuses.items() if k != "success"),
        status_counts=dict(statuses),
        attempt_directories_without_record=len(attempts) - len(records),
        recorded_evaluation_elapsed_seconds=sum(r.get("elapsed_seconds", 0)
                                              for r in records.values()),
        cache_hits=sum(bool(r.get("cache_hit")) for r in records.values()),
    )


def checked_record(embedded, directory, official, issues):
    """Use the on-disk official record; do not trust an embedded score alone."""
    if not embedded:
        return None
    name = embedded.get("record_path")
    path = Path(name) if name else None
    # Relocated result packages can retain their old absolute record paths.
    if path is not None and str(path) not in official:
        attempt = embedded.get("attempt_id")
        matches = [Path(p) for p in official if Path(p).parent.name == attempt]
        path = matches[0] if len(matches) == 1 else path
    actual = official.get(str(path)) if path is not None else None
    if actual is None:
        if embedded.get("status") == "success":
            issues.append(f"successful summary record missing on disk: {directory}: {name}")
        return None
    if actual.get("status") != embedded.get("status") or score(actual) != score(embedded):
        issues.append(f"summary/disk result mismatch: {path}")
        return None
    return actual


def analyze(inputs, out):
    manifest = read(inputs)
    seeds = manifest["seeds"]
    if len({s["id"] for s in seeds}) != len(seeds):
        raise ValueError("duplicate seed ids")
    cases = sorted({s["case"] for s in seeds})
    selected = {}
    for seed in seeds:
        threshold = seed["selected_makespan"], seed["selected_added_copy_bytes"]
        if seed["case"] in selected and selected[seed["case"]] != threshold:
            raise ValueError("inconsistent selected threshold for one graph")
        selected[seed["case"]] = threshold
    protocol = read(out / "protocol.json") if (out / "protocol.json").exists() else {}
    progress = read(out / "progress.json") if (out / "progress.json").exists() else {}
    issues, arm_rows, paired, seed_rows = [], [], [], []
    seed_official, seed_cost = disk_cost(out / "seeds")
    arm_official, candidate_cost = disk_cost(out / "arms")
    best_new = {}
    for seed in seeds:
        seed_dir = out / "seeds" / seed["id"]
        seed_path = seed_dir / "seed.json"
        replay = read(seed_path) if seed_path.exists() else {}
        seed_record = checked_record(replay.get("record"), seed_dir, seed_official, issues)
        seed_value = score(seed_record)
        expected = seed["expected_makespan"], seed["expected_added_copy_bytes"]
        verified = bool(replay.get("verified") and seed_value == expected)
        if replay.get("verified") and not verified:
            issues.append(f"seed verification/metrics inconsistent: {seed['id']}")
        seed_rows.append(dict(id=seed["id"], case=seed["case"], kind=seed["kind"],
                              complete=bool(replay.get("complete")), verified=verified,
                              expected_score=list(expected), replay_score=seed_value,
                              replay_wall_seconds=replay.get("replay_wall_seconds"),
                              error=replay.get("error")))
        pair_arms = {}
        for family in FAMILIES:
            arm_dir = out / "arms" / seed["id"] / family
            summary_path = arm_dir / "summary.json"
            state = read(summary_path) if summary_path.exists() else {}
            calls = state.get("calls", [])
            completed = bool(state.get("complete"))
            pending = state.get("pending") is not None
            statuses = Counter(c.get("record", {}).get("status", "unknown") for c in calls)
            generations = Counter(g.get("status", "unknown") for g in state.get("generations", []))
            best = seed_record if verified else None
            referenced = set()
            improvements = 0
            for call in calls:
                actual = checked_record(call.get("record"), arm_dir, arm_official, issues)
                if actual:
                    referenced.add(actual.get("record_path"))
                value = score(actual)
                if value is not None and verified and (score(best) is None or value < score(best)):
                    best = actual
                    improvements += 1
                # New records are candidate evaluations, not inherited seed values.
                if value is not None and verified and value < selected[seed["case"]]:
                    prior = best_new.get(seed["case"])
                    if prior is None or value < tuple(prior["score"]):
                        old = selected[seed["case"]]
                        best_new[seed["case"]] = dict(
                            case=seed["case"], seed_id=seed["id"], family=family,
                            name=call.get("name"), score=list(value), selected_score=list(old),
                            time_reduction_vs_selected=reduction(old[0], value[0]),
                            added_copy_delta_vs_selected=value[1] - old[1],
                            improvement_type="time" if value[0] < old[0] else "same_time_less_copy",
                            record_path=actual.get("record_path"), plan_path=actual.get("plan_path"),
                            independent_acceptance_replay_required=True,
                        )
            actual_best = score(best)
            reported_best = score(state.get("best_record"))
            if state and verified and reported_best != actual_best:
                issues.append(f"best summary differs from recomputed seed/calls: {seed['id']}/{family}")
            arm_disk = {p: r for p, r in arm_official.items() if Path(p).is_relative_to(arm_dir)}
            unaccounted = sum(r.get("record_path") not in referenced for r in arm_disk.values())
            if completed and (pending or unaccounted):
                issues.append(f"completed arm has pending/unaccounted result: {seed['id']}/{family}")
            threshold = selected[seed["case"]]
            row = dict(
                seed_id=seed["id"], case=seed["case"], kind=seed["kind"], family=family,
                complete=completed, seed_verified=verified, pending=pending,
                completed_logical_calls=len(calls), paid_slots_including_pending=len(calls) + int(pending),
                official_recorded_calls=len(arm_disk), official_records_not_yet_in_summary=unaccounted,
                logical_failures=sum(n for s, n in statuses.items() if s != "success"),
                call_status_counts=dict(statuses),
                generation_timeouts=generations.get("generation_timeout", 0),
                generation_errors=generations.get("generation_error", 0),
                generation_exhausted=generations.get("exhausted", 0),
                generation_seconds=sum(g.get("seconds", 0) for g in state.get("generations", [])),
                elapsed_seconds=state.get("elapsed_seconds"),
                scheduling_budget_overrun_seconds=state.get("scheduling_budget_overrun_seconds"),
                stop_reason=state.get("stop_reason", "not_started"), runner_error=state.get("error"),
                seed_score=list(expected), best_score=actual_best, selected_score=list(threshold),
                best_makespan=actual_best[0] if actual_best else None,
                best_added_copy_bytes=actual_best[1] if actual_best else None,
                time_reduction_vs_seed=reduction(expected[0], actual_best[0]) if actual_best else None,
                time_reduction_vs_selected=reduction(threshold[0], actual_best[0]) if actual_best else None,
                added_copy_delta_vs_selected=actual_best[1] - threshold[1] if actual_best else None,
                score_relation_vs_selected=relation(actual_best, threshold),
                strict_score_updates_from_seed=improvements,
                best_record_path=best.get("record_path") if best else None,
                summary_path=str(summary_path),
            )
            arm_rows.append(row)
            pair_arms[family] = row
        old, new = pair_arms["legacy_joint"], pair_arms["region_joint"]
        valid_pair = old["complete"] and new["complete"] and verified and old["best_score"] and new["best_score"]
        pair = dict(seed_id=seed["id"], case=seed["case"], kind=seed["kind"],
                    pair_complete=bool(valid_pair), seed_makespan=expected[0],
                    seed_added_copy_bytes=expected[1], selected_makespan=selected[seed["case"]][0],
                    selected_added_copy_bytes=selected[seed["case"]][1])
        for family, row in pair_arms.items():
            for key in ("complete", "pending", "completed_logical_calls", "official_recorded_calls",
                        "logical_failures", "generation_timeouts", "generation_errors", "elapsed_seconds",
                        "generation_seconds", "stop_reason", "best_makespan", "best_added_copy_bytes",
                        "time_reduction_vs_seed", "time_reduction_vs_selected",
                        "added_copy_delta_vs_selected", "score_relation_vs_selected"):
                pair[family + "_" + key] = row[key]
        pair["legacy_score_vs_region"] = relation(old["best_score"], new["best_score"]) if valid_pair else "unfinished_or_invalid"
        pair["legacy_time_reduction_vs_region"] = reduction(new["best_makespan"], old["best_makespan"]) if valid_pair else None
        paired.append(pair)
    batch_finished = bool(progress.get("complete")) and all(r["complete"] for r in arm_rows) and all(r["complete"] for r in seed_rows)
    pending_count = sum(r["pending"] for r in arm_rows)
    final = bool(batch_finished and not pending_count and not issues)
    # Recordless interrupted requests remain charged logical slots, not successes.
    seed_cost.update(expected_seed_replays=len(seeds), verified_seed_replays=sum(r["verified"] for r in seed_rows),
                     replay_wall_seconds_sum=sum(r["replay_wall_seconds"] or 0 for r in seed_rows))
    candidate_cost.update(completed_logical_calls=sum(r["completed_logical_calls"] for r in arm_rows),
                          logical_failures=sum(r["logical_failures"] for r in arm_rows), pending_slots=pending_count,
                          generation_timeouts=sum(r["generation_timeouts"] for r in arm_rows),
                          generation_errors=sum(r["generation_errors"] for r in arm_rows),
                          generation_seconds_sum=sum(r["generation_seconds"] for r in arm_rows),
                          arm_elapsed_seconds_sum=sum(r["elapsed_seconds"] or 0 for r in arm_rows))
    outcomes = Counter(p["legacy_score_vs_region"] for p in paired if p["pair_complete"])
    result = dict(
        status="final" if final else "provisional", analysis_final=final, batch_finished=batch_finished,
        scope="Warm relay diagnostic on historical starts; not a cold-start solver comparison or 100-graph score.",
        score_definition="Lexicographic (makespan, added_copy_bytes); time first, COPY only breaks time ties.",
        sample=dict(independent_graphs=len(cases), graphs=cases, correlated_starts=len(seeds),
                    planned_arms=len(seeds) * 2, complete_arms=sum(r["complete"] for r in arm_rows),
                    note="Six starts on three graphs are not six independent graph samples."),
        protocol=protocol, integrity_issues=issues,
        costs=dict(shared_seed_replays=seed_cost, candidate_arms=candidate_cost,
                   official_recorded_calls_total=seed_cost["official_recorded_calls"] + candidate_cost["official_recorded_calls"],
                   official_recorded_failures_total=seed_cost["failures"] + candidate_cost["failures"],
                   note="Seed replays counted once, never once per arm. Sum of arm wall seconds is not concurrent batch elapsed time. Historical acquisition and later acceptance replays excluded."),
        paired=dict(completed_valid_pairs=sum(p["pair_complete"] for p in paired),
                    legacy_vs_region_score=dict(outcomes), comparisons=paired),
        new_best_candidates=sorted(best_new.values(), key=lambda r: r["case"]),
        new_best_candidates_are_accepted=False,
        seeds=seed_rows, arms=arm_rows,
    )
    return result, paired


def main():
    base = Path(__file__).resolve().parent / "P1接力实验_20260926"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, default=base / "inputs" / "manifest.json")
    parser.add_argument("--out", type=Path, default=base / "run_v1")
    args = parser.parse_args()
    out = args.out.resolve()
    result, rows = analyze(args.inputs.resolve(), out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "分析汇总.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (out / "逐起点对照.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(dict(status=result["status"], sample=result["sample"],
                          official_recorded_calls=result["costs"]["official_recorded_calls_total"],
                          official_recorded_failures=result["costs"]["official_recorded_failures_total"],
                          new_best_candidates=len(result["new_best_candidates"]),
                          integrity_issues=result["integrity_issues"]), ensure_ascii=False))


if __name__ == "__main__":
    main()
