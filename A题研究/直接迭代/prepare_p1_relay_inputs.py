"""Recover six read-only, provenance-checked P1 relay seeds from a Git delivery.

This does not call the official simulator. Plans are selected from exact archive
members, never extracted wholesale. Existing differing output is not overwritten.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from common_run import DATA, GraphIR, validate_plan
from solver.common import object_digest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
REVISION = "fea72e66e7260ba4deecbc28569b641e590f5046"
DELIVERY = "A题研究/直接迭代/持续联合冲刺_20260926"
AUDIT = DELIVERY + "/正式v2完整审计"
SELECTED = DELIVERY + "/最终精选1500"
CASES = ("case_047", "case_075", "case_085")


def git_bytes(path: str) -> bytes:
    return subprocess.check_output(["git", "show", f"{REVISION}:{path}"], cwd=ROOT)


def archive_members(paths: list[str], wanted: set[str]) -> dict[str, dict]:
    """Concatenate Git blobs in a temporary stream; parse named JSON files only."""
    plans = {}
    with tempfile.TemporaryFile() as archive:
        for path in paths:
            with subprocess.Popen(
                ["git", "show", f"{REVISION}:{path}"], cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ) as process:
                assert process.stdout is not None
                shutil.copyfileobj(process.stdout, archive)
                stderr = process.stderr.read() if process.stderr else b""
                if process.wait() != 0:
                    raise RuntimeError(f"Cannot read Git blob {path}: {stderr.decode()}")
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode="r|gz") as stream:
            for member in stream:
                if member.name not in wanted:
                    continue
                if not member.isfile() or member.name in plans:
                    raise ValueError(f"Unexpected or duplicate member: {member.name}")
                fileobj = stream.extractfile(member)
                assert fileobj is not None
                plans[member.name] = json.load(fileobj)
        if set(plans) != wanted:
            raise ValueError(f"Missing archive members: {sorted(wanted - set(plans))}")
    return plans


def write_unchanged_or_new(path: Path, value: dict) -> None:
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    if path.exists():
        if path.read_bytes() != encoded:
            raise FileExistsError(f"Refusing to overwrite differing output: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as out:
        out.write(encoded)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE / "P1接力实验_20260926" / "inputs")
    args = parser.parse_args()
    output = args.output.resolve()

    ledger_path = AUDIT + "/all_paid_calls_and_recovery.json.gz"
    ledger = json.loads(gzip.decompress(git_bytes(ledger_path)))
    score_path = SELECTED + "/累计1500配置成绩.csv"
    rows = list(csv.DictReader(io.StringIO(git_bytes(score_path).decode("utf-8-sig"))))
    selected = {
        row["case"]: row for row in rows
        if row["case"] in CASES and row["problem"] == "1" and row["cores"] == "5"
    }
    if set(selected) != set(CASES):
        raise ValueError("Latest selected package does not contain all requested seeds")

    calls = {}
    arms = {}
    for case in CASES:
        matching = [arm for arm in ledger if (
            arm["case"] == case and arm["problem"] == 1
            and arm["cores"] == 5 and arm["variant"] == "persistent"
        )]
        if len(matching) != 1:
            raise ValueError(f"Expected one formal persistent arm for {case}")
        arm = matching[0]
        candidates = [call for call in arm["summary"]["calls"] if (
            call["phase"] == "joint" and call["record"]["status"] == "success"
        )]
        if case == "case_075":
            matching_call = [call for call in candidates if call["paid_call_index"] == 9]
            if len(matching_call) != 1 or matching_call[0]["record"]["metrics"]["makespan"] != 724319:
                raise ValueError("075 call 9 no longer matches the specified intermediate seed")
            call = matching_call[0]
        else:
            call = min(candidates, key=lambda candidate: (
                candidate["record"]["metrics"]["makespan"],
                candidate["record"]["metrics"]["data_movement_bytes"]["added_copy_bytes"],
                candidate["paid_call_index"],
            ))
        calls[case], arms[case] = call, arm

    parts = json.loads(git_bytes(AUDIT + "/all_evaluated_plans.parts.json"))["parts"]
    for part in parts:
        if Path(part["name"]).name != part["name"]:
            raise ValueError("Unexpected archive part path")
    paid_plans = archive_members(
        [AUDIT + "/" + part["name"] for part in parts],
        {call["archived_plan"] for call in calls.values()},
    )
    selected_plans = archive_members(
        [SELECTED + "/selected_plans.tar.gz"],
        {row["plan"] for row in selected.values()},
    )

    seeds = []
    pending_plans = []
    for case in CASES:
        ir = GraphIR.from_path(DATA / (case + ".json"))
        call, row, arm = calls[case], selected[case], arms[case]
        record = call["record"]
        parent_candidates = [entry for entry in arm["summary"]["calls"]
                             if entry["record"].get("record_path") == call["parent_record"]]
        if len(parent_candidates) != 1:
            raise ValueError(f"Cannot uniquely trace parent record for {case}")
        parent = parent_candidates[0]
        initialization_best = min(
            entry["record"]["metrics"]["makespan"] for entry in arm["summary"]["calls"]
            if entry["phase"] == "initialization" and entry["record"]["status"] == "success"
        )
        for kind in ("region", "selected"):
            seed_id = f"{case}_{kind}"
            plan_path = output / (seed_id + ".json")
            if kind == "region":
                plan = paid_plans[call["archived_plan"]]
                expected_signature = record["hashes"]["plan_sha256"]
                if call["plan_signature"] != expected_signature:
                    raise ValueError(f"Ledger signature inconsistency for {seed_id}")
                makespan = record["metrics"]["makespan"]
                added_copy = record["metrics"]["data_movement_bytes"]["added_copy_bytes"]
                source = {
                    "ledger": ledger_path,
                    "config_id": arm["config_id"],
                    "variant": "persistent",
                    "paid_call_index": call["paid_call_index"],
                    "candidate_name": call["name"],
                    "family": call["metadata"].get("family"),
                    "archived_plan": call["archived_plan"],
                    "record_path": record["record_path"],
                    "parent_record": call["parent_record"],
                    "parent_paid_call_index": parent["paid_call_index"],
                    "parent_makespan": parent["record"]["metrics"]["makespan"],
                    "parent_added_copy_bytes": parent["record"]["metrics"]["data_movement_bytes"]["added_copy_bytes"],
                    "initialization_best_makespan": initialization_best,
                    "improves_initialization_best": makespan < initialization_best,
                    "historical_accepted": call["accepted"],
                    "historical_local_accepted": call["local_accepted"],
                    "selection_rule": "specified call 9 intermediate" if case == "case_075" else "best successful joint-region call by time then added COPY",
                }
            else:
                plan = selected_plans[row["plan"]]
                expected_signature = row["plan_sha256"]
                makespan, added_copy = int(row["makespan"]), int(row["added_copy"])
                source = {
                    "score_table": score_path,
                    "archive": SELECTED + "/selected_plans.tar.gz",
                    "archived_plan": row["plan"],
                    "method": row["source"],
                    "source_commit": row["source_commit"],
                    "source_plan": row["source_plan"],
                    "verification": row["verification"],
                }
            validate_plan(ir, plan)
            if len(plan["core_schedules"]) != 5:
                raise ValueError(f"Wrong core count for {seed_id}")
            if object_digest(plan) != expected_signature:
                raise ValueError(f"Plan does not match original ledger identity: {seed_id}")
            pending_plans.append((plan_path, plan))
            seeds.append({
                "id": seed_id, "case": case, "kind": kind, "problem": 1, "cores": 5,
                "plan_path": str(plan_path), "graph_path": str(ir.path),
                "expected_makespan": makespan, "expected_added_copy_bytes": added_copy,
                "selected_makespan": int(row["makespan"]),
                "selected_added_copy_bytes": int(row["added_copy"]),
                "validation": {"structurally_legal": True, "core_count": 5, "original_plan_identity_matches": True,
                               "official_evaluation_rerun": False},
                "source": source,
            })

    manifest = {
        "description": "P1 warm relay diagnostic: three recovered region candidates and three latest selected strong plans",
        "source_revision": REVISION,
        "scope": "Historical results are expected replay values, not fresh local evaluation results or a cold algorithm comparison.",
        "note_case_047": "Best joint-region candidate 322588 is slower than initialization best 322435; retained transparently to diagnose whether phase can repair this explicit region candidate.",
        "seeds": seeds,
    }
    # Check every destination before writing any new output.
    outputs = pending_plans + [(output / "manifest.json", manifest)]
    for path, value in outputs:
        encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        if path.exists() and path.read_bytes() != encoded:
            raise FileExistsError(f"Refusing to overwrite differing output: {path}")
    for path, value in outputs:
        write_unchanged_or_new(path, value)
    for seed in seeds:
        print(f"{seed['id']}: makespan={seed['expected_makespan']}, added_COPY={seed['expected_added_copy_bytes']}")
    print(f"Manifest: {output / 'manifest.json'}")
    print("PASS: six complete legal five-core plans, each matching original archived plan identity; no evaluator calls.")


if __name__ == "__main__":
    main()
