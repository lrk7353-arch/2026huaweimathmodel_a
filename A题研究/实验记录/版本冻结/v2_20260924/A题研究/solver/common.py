"""Small stdlib helpers; all generated artifacts stay outside official inputs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DATA = ROOT / "选题分析" / "A题附件" / "data"
OFFICIAL = DATA.parent / "code"


def read_json(path):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError(f"Duplicate JSON key: {key}")
            obj[key] = value
        return obj
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
            out.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def object_digest(obj):
    # Conservatively preserve even object insertion order, matching the worker's
    # cache key. No unproved equivalence of evaluator input orders is assumed.
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def source_manifest():
    return {str(p.relative_to(ROOT)): digest(p)
            for base in (HERE, OFFICIAL) for p in sorted(base.glob("*.py"))}


def active_cores(plan):
    return sum(bool(schedule) for schedule in plan["core_schedules"])


def single_active_plan(graph, num_cores):
    if num_cores < 1:
        raise ValueError("num_cores must be positive")
    ids = sorted(op["id"] for op in graph["ops"]
                 if op["op"] not in {"COPY_IN", "COPY_OUT"})
    return {"node_to_subgraph": {str(i): 0 for i in ids},
            "core_schedules": [[0] if ids else []] + [[] for _ in range(num_cores - 1)]}
