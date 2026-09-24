#!/usr/bin/env python3
"""Solve a fresh A graph with an audited P1/P2/P3 portfolio (engineering defaults)."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from controller import Solver, objective


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("graph", type=Path)
    p.add_argument("-n", "--num-cores", type=int, choices=range(1, 6), required=True)
    p.add_argument("-p", "--problem", type=int, choices=(1, 2, 3), required=True)
    p.add_argument("--config", type=Path)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--evaluation-dir", type=Path)
    p.add_argument("--incumbent-plan", type=Path)
    p.add_argument("--wcc-policy", choices=("mixed", "protected", "unrestricted"), default="mixed")
    p.add_argument("-o", "--output", type=Path)
    for stage, default in (("component", 6), ("operation", 12), ("selective", 18), ("wcc", 9), ("trace", 30), ("cache", 18)):
        p.add_argument("--" + stage + "-cap", type=int, default=default)
    p.add_argument("--round-width", type=int, default=6)
    p.add_argument("--max-rounds", type=int, default=5)
    p.add_argument("--max-evaluations", type=int, default=90)
    p.add_argument("--seed", type=int, default=17)
    p.add_argument("--timeout", type=float, help="Per evaluation; default 60s for <=10000 non-COPY ops, otherwise 180s")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        search = Solver(**vars(args))
        out = search.run()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps({"status": out["status"], "completed": out["completed"], "logical_calls": out["logical_calls"],
        "stop_reason": out["stop_reason"], "best": list(objective(out["best"]["record"])) if out["best"] else None,
        "summary": str(search.run_dir / "summary.json")}, ensure_ascii=False))
    return 0 if out["status"] == "success" and out["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
