#!/usr/bin/env python3
"""Read-only environment inventory; does not compile, launch kernels, or change settings."""
import glob
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

def run_readonly(argv, timeout=8):
    try:
        r = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout, check=False)
        return {"command": argv, "returncode": r.returncode, "output": r.stdout[:12000]}
    except subprocess.TimeoutExpired:
        return {"command": argv, "status": "timeout", "timeout_seconds": timeout}
    except OSError as exc:
        return {"command": argv, "status": "unavailable", "error": str(exc)}

def main():
    names = ("python3", "npu-smi", "bisheng", "ccec", "msprof", "msopprof", "cmake", "g++")
    commands = {name: shutil.which(name) for name in names}
    distributions = {}
    for name in ("torch", "torch-npu", "numpy", "triton-ascend"):
        try:
            distributions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            distributions[name] = None
    known_roots = [Path("/usr/local/Ascend"), Path.home() / "Ascend"]
    version_files = {}
    for root in known_roots:
        for pattern in ("ascend-toolkit/latest/version.cfg",
                        "ascend-toolkit/latest/*-linux/ascend_toolkit_install.info",
                        "driver/version.info", "cann-*/version.cfg",
                        "cann-*/*-linux/ascend_toolkit_install.info"):
            for path in sorted(root.glob(pattern)):
                if path.is_file():
                    try:
                        # Installation/version metadata only. No environment dump or credentials.
                        text = path.read_text(errors="replace")
                        allowed = [line for line in text.splitlines()
                                   if any(key in line.lower() for key in
                                          ("version", "arch=", "os="))]
                        version_files[str(path)] = allowed[:40]
                    except OSError:
                        version_files[str(path)] = ["unreadable"]
    report = {
        "scope": "environment inventory only; NOT a successful NPU execution or benchmark",
        "system": {"os": platform.system(), "architecture": platform.machine(),
                   "python": platform.python_version()},
        "commands": commands,
        "python_distributions_not_imported": distributions,
        "device_nodes": sorted(set(glob.glob("/dev/davinci*") +
                                   glob.glob("/dev/devmm_svm") +
                                   glob.glob("/dev/hisi_hdc"))),
        "ascend_path_hints": {key: os.environ.get(key) for key in
                             ("ASCEND_HOME_PATH", "ASCEND_TOOLKIT_HOME", "ASCEND_OPP_PATH")},
        "version_files": version_files,
        "device_query": run_readonly([commands["npu-smi"], "info"])
                        if commands["npu-smi"] else {"status": "npu-smi not on PATH"},
        "interpretation": "Missing commands may mean an unsourced CANN environment. "
                          "Visible devices and npu-smi success do not prove ACL/kernel execution works.",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
