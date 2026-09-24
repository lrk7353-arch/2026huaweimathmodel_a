#!/usr/bin/env python3
"""Complete-denominator metrics for a verified best-known plan portfolio."""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import statistics
import sys

RESEARCH = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(RESEARCH), str(Path(__file__).resolve().parent)]
from solver.common import atomic_json, digest, read_json
from summarize_formal import baselines, write_csv


def main(portfolio):
    portfolio = Path(portfolio).resolve()
    manifest = read_json(portfolio / "manifest.json")
    if manifest["rejected"]:
        raise ValueError("collector rejected candidate evidence")
    with (portfolio / "catalog.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    baseline = baselines()
    groups = defaultdict(dict)
    enriched = []
    for row in rows:
        p, n, case, value = int(row["problem"]), int(row["num_cores"]), row["case"], int(row["makespan"])
        if case in groups[p, n]:
            raise ValueError("duplicate portfolio slot")
        if digest(row["plan_path"]) != row["plan_sha256"]:
            raise ValueError("portfolio plan changed")
        item = {**row, "original_singlecore_makespan": baseline[case],
                "diagnostic_baseline_over_time": baseline[case] / value,
                "official_speedup": 1 if p in (1, 2) and n == 1 else baseline[case] / value}
        groups[p, n][case] = item
        enriched.append(item)
    stats = []
    for p in (1, 2, 3):
        for n in range(1, 6):
            items = groups[p, n]
            complete = set(items) == set(baseline)
            stats.append({"problem": p, "num_cores": n, "completed": len(items), "expected": 100,
                          "all_100": complete,
                          "mean_official_speedup": statistics.mean(x["official_speedup"] for x in items.values()) if complete else None,
                          "mean_diagnostic_baseline_over_time": statistics.mean(x["diagnostic_baseline_over_time"] for x in items.values()) if complete else None,
                          "total_added_copy_bytes": sum(int(x["added_copy_bytes"]) for x in items.values()) if complete else None})
    write_csv(portfolio / "metrics_by_case.csv", enriched)
    write_csv(portfolio / "metrics_by_group.csv", stats)
    value = {"scope": "verified best known across historical and extra-budget runs, not equal-budget algorithm result",
             "portfolio_manifest_sha256": digest(portfolio / "manifest.json"),
             "catalog_sha256": digest(portfolio / "catalog.csv"), "source_sha256": digest(__file__),
             "selected_count": len(rows), "full_coverage": len(rows) == 1500,
             "normalization": "original official singlecore B divided by same-case time; P1/P2 N1 official speedup fixed to1",
             "partial_groups_have_no_aggregate": True, "groups": stats}
    atomic_json(portfolio / "metrics_summary.json", value)
    print(json.dumps({"selected_count": len(rows), "complete_groups": [s for s in stats if s["all_100"]]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("portfolio", type=Path)
    main(parser.parse_args().portfolio)
