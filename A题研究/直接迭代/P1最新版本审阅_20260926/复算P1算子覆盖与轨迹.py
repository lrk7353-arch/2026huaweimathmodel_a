#!/usr/bin/env python3
"""Read committed evidence and reproduce P1 coverage/convergence CSVs.

No solver/evaluator is imported or executed. The only writes are the two CSVs
in --output-dir. `arm_best` always means best makespan observed so far within
one experimental arm; it never means a historical portfolio record.

Run from any directory with Python 3:
    python3 复算P1算子覆盖与轨迹.py
"""
import argparse
import csv
import gzip
import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path


SOURCE_REVISION = "fea72e66e7260ba4deecbc28569b641e590f5046"
SOURCE_PATH = (
    "A题研究/直接迭代/持续联合冲刺_20260926/正式v2完整审计/"
    "all_paid_calls_and_recovery.json.gz"
)
SCOPE = "current experimental arm only; not historical portfolio best"


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--source-revision", default=SOURCE_REVISION)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    source = args.source_revision + ":" + SOURCE_PATH
    packed = subprocess.check_output(["git", "-C", str(args.repo), "show", source])
    evidence = json.loads(gzip.decompress(packed))
    arms = [a for a in evidence if a["problem"] == 1 and a["cores"] == 5
            and a["variant"] in ("legacy", "persistent")]
    assert len(arms) == 26, f"Expected 13 graphs x 2 arms, found {len(arms)}"
    assert len({a["case"] for a in arms}) == 13
    coverage = defaultdict(Counter)
    trace = []
    for arm in sorted(arms, key=lambda a: (a["case"], a["variant"])):
        best = None
        for index, call in enumerate(arm["summary"]["calls"], 1):
            record = call.get("record", {})
            metrics = record.get("metrics", {})
            makespan = metrics.get("makespan")
            success = record.get("status") == "success" and makespan is not None
            improved = bool(success and (best is None or makespan < best))
            previous_best = best
            if success:
                best = makespan if best is None else min(best, makespan)
            family = call.get("metadata", {}).get("family") or "(none)"
            key = (arm["variant"], call["phase"], family)
            coverage[key]["paid_calls"] += 1
            coverage[key]["successful_calls"] += int(success)
            coverage[key]["strict_arm_best_updates"] += int(improved)
            if arm["case"] in ("case_047", "case_075"):
                trace.append(dict(
                    source_revision=args.source_revision,
                    config_id=arm["config_id"], case=arm["case"],
                    problem=1, cores=5, variant=arm["variant"], call_index=index,
                    phase=call["phase"], family=family, name=call["name"],
                    status=record.get("status"), makespan=makespan,
                    arm_best_before=previous_best, arm_best_after=best,
                    strict_arm_best_update=improved,
                    arm_best_update_scope=SCOPE,
                    added_copy_bytes=metrics.get("data_movement_bytes", {}).get("added_copy_bytes"),
                    evaluation_elapsed_seconds=record.get("elapsed_seconds"),
                ))
    # Explicit zero rows make the old/new neighborhood difference inspectable.
    for variant in ("legacy", "persistent"):
        for family in ("phase", "tensor_cap", "affinity", "branch"):
            coverage[(variant, "joint", family)]
    coverage_rows = []
    for (variant, phase, family), stats in sorted(coverage.items()):
        coverage_rows.append(dict(
            source_revision=args.source_revision, problem=1, cores=5,
            graph_count=13, variant=variant, phase=phase, family=family,
            paid_calls=stats["paid_calls"], successful_calls=stats["successful_calls"],
            strict_arm_best_updates=stats["strict_arm_best_updates"],
            arm_best_update_scope=SCOPE,
        ))
    expected = {
        ("legacy", "joint", "phase"): (111, 70),
        ("legacy", "joint", "tensor_cap"): (24, 2),
        ("persistent", "joint", "affinity"): (79, 9),
        ("persistent", "joint", "branch"): (29, 3),
        ("persistent", "joint", "phase"): (0, 0),
        ("persistent", "joint", "tensor_cap"): (0, 0),
    }
    if args.source_revision == SOURCE_REVISION:
        for key, expected_counts in expected.items():
            actual = coverage[key]
            assert (actual["paid_calls"], actual["strict_arm_best_updates"]) == expected_counts
        for case, variant, expected_best in [
            ("case_047", "legacy", 160151), ("case_047", "persistent", 317205),
            ("case_075", "legacy", 424934), ("case_075", "persistent", 678468),
        ]:
            selected = [r for r in trace if r["case"] == case and r["variant"] == variant]
            assert selected[-1]["arm_best_after"] == expected_best
    args.output_dir.mkdir(parents=True, exist_ok=True)
    coverage_path = args.output_dir / "P1算子覆盖_13图五核.csv"
    trace_path = args.output_dir / "P1_047_075逐调用收敛轨迹.csv"
    write_csv(coverage_path, coverage_rows)
    write_csv(trace_path, trace)
    print(json.dumps(dict(source=source, graphs=13, experimental_arms=26,
                         coverage_rows=len(coverage_rows), trace_rows=len(trace),
                         scope=SCOPE, outputs=[str(coverage_path), str(trace_path)]),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
