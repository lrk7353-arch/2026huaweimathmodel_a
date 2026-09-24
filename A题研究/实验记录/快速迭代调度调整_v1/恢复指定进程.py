#!/usr/bin/env python3
"""Inspect or resume one recorded parent process after exact identity checking."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import subprocess

RECEIPT = Path(__file__).resolve().with_name("暂停记录.json")


def field(pid, key):
    return subprocess.check_output(["ps", "-p", str(pid), "-o", key + "="],
        text=True, env={**os.environ, "LC_ALL": "C"}).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume-pid", type=int, help="Resume exactly one recorded parent; omission is read-only.")
    args = parser.parse_args()
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    if args.resume_pid is None:
        for row in receipt["targets"]:
            try:
                same = field(row["pid"], "command") == row["command"] and field(row["pid"], "lstart") == row["started"]
                state = field(row["pid"], "stat") if same else "identity_changed"
            except subprocess.CalledProcessError:
                state = "exited"
            print(row["pid"], row["role"], state)
        return
    matches = [row for row in receipt["targets"] if row["pid"] == args.resume_pid]
    if len(matches) != 1 or args.resume_pid <= 0:
        parser.error("PID is not an explicitly recorded positive parent PID")
    row = matches[0]
    if field(row["pid"], "command") != row["command"] or field(row["pid"], "lstart") != row["started"]:
        raise RuntimeError("Process identity changed; refusing to signal a reused PID")
    if "T" not in field(row["pid"], "stat"):
        raise RuntimeError("Recorded process is not stopped; no signal sent")
    os.kill(row["pid"], signal.SIGCONT)
    row.update(action="SIGCONT_sent", resumed_at=datetime.now().astimezone().isoformat())
    receipt["status"] = "selectively_resumed"
    temp = RECEIPT.with_suffix(".tmp")
    temp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, RECEIPT)
    print("Resumed recorded parent PID", row["pid"])


if __name__ == "__main__":
    main()
