#!/usr/bin/env python3
"""One-shot CANN inventory and bounded ACL query. No installs, servers, or kernels."""
import argparse
import ctypes
import glob
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import time

def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def invoke(argv, timeout=10):
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", timeout=timeout, check=False)
        return {"argv": argv, "returncode": result.returncode, "output": result.stdout[:24000]}
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or b""
        return {"argv": argv, "status": "timeout",
                "output": raw.decode(errors="replace")[:24000] if isinstance(raw, bytes) else raw[:24000]}
    except OSError as exc:
        return {"argv": argv, "status": "unavailable", "error": str(exc)}

def toolkit_roots():
    candidates = [Path.home() / "Ascend/cann-9.0.0",
                  Path.home() / "Ascend/ascend-toolkit/latest",
                  Path("/usr/local/Ascend/ascend-toolkit/latest")]
    for key in ("ASCEND_HOME_PATH", "ASCEND_TOOLKIT_HOME"):
        if os.environ.get(key):
            candidates.insert(0, Path(os.environ[key]))
    for name in ("bisheng", "ccec"):
        binary = shutil.which(name)
        if binary:
            candidates.append(Path(binary).parent.parent)
    return list(dict.fromkeys(str(p.resolve()) for p in candidates if p.is_dir()))

def find_assets(roots):
    names = {"libascendcl.so", "kernel_operator.h", "acl.h", "ascendc.cmake",
             "ascendc-config.cmake", "ASCConfig.cmake", "version.cfg",
             "ascend_toolkit_install.info", "set_env.sh"}
    found = []
    visited = 0
    for root in roots:
        for folder, dirs, files in os.walk(root, followlinks=False):
            visited += 1
            rel = Path(folder).relative_to(root)
            dirs[:] = [d for d in dirs if d not in {".git", ".ssh", "__pycache__", "node_modules"}]
            if len(rel.parts) >= 6:
                dirs[:] = []
            if visited > 6000:
                return {"files": found, "truncated": True}
            for filename in files:
                if filename in names:
                    found.append(str(Path(folder) / filename))
    return {"files": list(dict.fromkeys(found)), "truncated": False}

def acl_worker(library):
    # Separate process: ACL initialization/query may depend on driver/container permissions.
    info = {"scope": "runtime query only; no SetDevice, no allocation, no kernel"}
    lib = ctypes.CDLL(library)
    lib.aclInit.argtypes = [ctypes.c_char_p]
    lib.aclInit.restype = ctypes.c_int
    lib.aclrtGetDeviceCount.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
    lib.aclrtGetDeviceCount.restype = ctypes.c_int
    lib.aclFinalize.argtypes = []
    lib.aclFinalize.restype = ctypes.c_int
    init = lib.aclInit(None)
    info["aclInit_returncode"] = init
    if init == 0:
        try:
            count = ctypes.c_uint32()
            info["aclrtGetDeviceCount_returncode"] = lib.aclrtGetDeviceCount(ctypes.byref(count))
            info["visible_device_count"] = count.value
            if hasattr(lib, "aclrtGetSocName"):
                lib.aclrtGetSocName.argtypes = []
                lib.aclrtGetSocName.restype = ctypes.c_char_p
                soc = lib.aclrtGetSocName()
                info["soc_name"] = soc.decode(errors="replace") if soc else None
        finally:
            info["aclFinalize_returncode"] = lib.aclFinalize()
    print(json.dumps(info, ensure_ascii=False))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--acl-worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.acl_worker:
        acl_worker(args.acl_worker)
        return
    out = (args.out or Path.home() / "ascend_experiments" /
           ("stage0_" + time.strftime("%Y%m%d_%H%M%S"))).resolve()
    out.mkdir(parents=True, exist_ok=False)
    print("Stage 0: collecting toolchain/runtime information; no operator benchmark.", flush=True)
    names = ("python3", "git", "curl", "npu-smi", "bisheng", "ccec", "msprof",
             "msopprof", "cmake", "g++")
    commands = {name: shutil.which(name) for name in names}
    packages = {}
    for name in ("torch", "torch-npu", "numpy"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    roots = toolkit_roots()
    report = {
        "stage": 0, "scope": "toolchain inventory and bounded ACL queries; NOT a benchmark",
        "system": {"os": platform.system(), "arch": platform.machine(), "python": platform.python_version()},
        "commands": commands, "packages_not_imported": packages, "toolkit_roots": roots,
        "devices": sorted(set(glob.glob("/dev/davinci*") + glob.glob("/dev/devmm_svm") +
                              glob.glob("/dev/hisi_hdc"))),
        "assets": find_assets(roots), "checks": {},
    }
    checks = report["checks"]
    if commands["npu-smi"]:
        checks["npu_smi"] = invoke([commands["npu-smi"], "info"], 10)
    for name in ("bisheng", "ccec", "cmake", "g++"):
        if commands[name]:
            checks[name] = invoke([commands[name], "--version"], 8)
    if commands["msprof"]:
        checks["msprof_help"] = invoke([commands["msprof"], "op", "--help"], 10)
    # Never load a toolkit stub library. Try the runtime library once; do not try devices by guessing.
    libraries = [p for p in report["assets"]["files"]
                 if Path(p).name == "libascendcl.so" and not any("stub" in part.lower() for part in Path(p).parts)]
    library = next(iter(libraries), "libascendcl.so")
    checks["acl_runtime"] = invoke([sys.executable, str(Path(__file__).resolve()), "--acl-worker", library], 30)
    report["notice"] = ("npu-smi IDs may differ from runtime logical IDs. Successful ACL queries do not "
                        "prove custom kernel compilation/execution or profiling permission.")
    source = Path(__file__).read_bytes()
    report["runner_sha256"] = hashlib.sha256(source).hexdigest()
    save_json(out / "report.json", report)
    (out / "stage0.py").write_bytes(source)
    (out / "README.txt").write_text(
        "Stage 0 results only. No operator was compiled or executed.\n"
        "Send this archive back for selecting the matching CANN build and runtime entry.\n"
        "No SSH configuration, credentials, full environment, or workspace source is collected.\n",
        encoding="utf-8")
    archive = out.with_name(out.name + ".tar.gz")
    with archive.open("xb") as raw:
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            tar.add(out, arcname=out.name)
    print("REPORT: " + str(out / "report.json"))
    print("RESULT_BUNDLE: " + str(archive))
    print("Complete. Missing tools/runtime errors are recorded, not treated as successful NPU execution.")

if __name__ == "__main__":
    main()
