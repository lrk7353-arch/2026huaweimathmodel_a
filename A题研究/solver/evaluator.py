"""Isolated, resumable access to the unchanged official evaluators (stdlib only).

``evaluate(..., problem=0, plan=None)`` uses the official single-core baseline.
Configuration defaults to ``graph_path.parent / 'config.txt'`` exactly as in the
official CLI. Synthetic graphs must therefore pass the official config explicitly.
Every invocation has an attempt directory, including cache hits and failures.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import traceback
import uuid


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value):
    # Preserve mapping insertion order as well as all list orders. The official
    # builder may iterate plan.items(); do not assume reordered objects are equal.
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix=".tmp-",
                                         suffix=".json", delete=False) as stream:
            temp = Path(stream.name)
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp), str(path))
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def _read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def _cached_record(path, key):
    try:
        record = _read_json(path)
        result_path = Path(record["result_path"])
        if (record.get("status") == "success" and record.get("cache_key") == key
                and result_path.is_file()
                and _sha256(result_path) == record.get("result_sha256")
                and isinstance(record.get("metrics", {}).get("makespan"), (int, float))):
            return record
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def evaluate(graph_path, plan, problem, run_dir, timeout=120.0,
             config_path=None, official_code=None):
    """Return a record; retain official result as gzip JSON only on success.

    ``elapsed_seconds`` measures this call, including hashing and cache access.
    ``evaluation_elapsed_seconds`` measures official evaluation in the originating
    worker, and remains available on cache hits. Worker RSS is host process memory,
    not modeled L1/UB; the latter is ``metrics.memory_peak_by_core``.
    """
    started = time.perf_counter()
    attempt_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex
    record = {
        "schema_version": 1, "attempt_id": attempt_id, "problem": problem,
        "status": "runtime_error", "metrics": {}, "elapsed_seconds": 0.0,
        "evaluation_elapsed_seconds": None, "cache_hit": False,
        "result_path": None, "plan_path": None, "hashes": {}, "error": None,
        "peak_memory_bytes": None, "returncode": None,
    }
    attempt = None
    try:
        run_dir = Path(run_dir).expanduser().resolve()
        attempt = run_dir / "attempts" / attempt_id
        attempt.mkdir(parents=True, exist_ok=False)
        record.update({"attempt_dir": str(attempt),
                       "record_path": str(attempt / "record.json"),
                       "stdout_path": str(attempt / "stdout.log"),
                       "stderr_path": str(attempt / "stderr.log")})
        # Create both logs even when failure occurs before worker launch.
        (attempt / "stdout.log").touch()
        (attempt / "stderr.log").touch()
        if type(problem) is not int or problem not in (0, 1, 2, 3):
            raise ValueError("problem must be 0, 1, 2, or 3")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if problem == 0 and plan is not None:
            record["status"] = "invalid_plan"
            raise ValueError("problem=0 requires plan=None (official whole-graph baseline)")
        if problem != 0:
            record["plan_path"] = str(attempt / "plan.json")
            try:
                _atomic_json(record["plan_path"], plan)
            except (TypeError, ValueError):
                record["status"] = "invalid_plan"
                raise
        graph_path = Path(graph_path).expanduser().resolve()
        config_path = (Path(config_path).expanduser().resolve() if config_path is not None
                       else graph_path.parent / "config.txt")
        default_code = Path(__file__).resolve().parents[2] / "选题分析/A题附件/code"
        official_code = (Path(official_code).expanduser().resolve() if official_code is not None
                         else default_code)
        worker = Path(__file__).resolve().with_name("eval_worker.py")
        record.update({"graph_path": str(graph_path), "config_path": str(config_path),
                       "official_code": str(official_code), "timeout_seconds": float(timeout)})
        if not official_code.is_dir():
            raise FileNotFoundError("official code directory not found: " + str(official_code))
        required_modules = ("singlecore_evaluate.py", "multicore_cut_evaluate_problem_1.py",
                            "multicore_cut_evaluate_problem_2.py", "multicore_cut_evaluate_problem_3.py",
                            "evaluation_validation.py", "contest_io.py")
        for name in required_modules:
            if not (official_code / name).is_file():
                raise FileNotFoundError("required official module not found: " + name)
        # Config never silently falls back to bundled values.
        if not config_path.is_file():
            raise FileNotFoundError("configuration file not found: " + str(config_path))
        hashes = {
            "graph_sha256": _sha256(graph_path),
            "plan_sha256": (_sha256(record["plan_path"]) if problem != 0
                            else hashlib.sha256(b"null").hexdigest()),
            "problem": problem, "config_sha256": _sha256(config_path),
            "official_py_sha256": {p.name: _sha256(p) for p in sorted(official_code.glob("*.py"))},
            "wrapper_sha256": _sha256(Path(__file__).resolve()),
            "worker_sha256": _sha256(worker),
            "python": sys.version, "python_executable": str(Path(sys.executable).resolve()),
            "platform": platform.platform(),
        }
        key = hashlib.sha256(_json_bytes(hashes)).hexdigest()
        record.update({"hashes": hashes, "cache_key": key})
        cache_path = run_dir / "cache" / key / "success.json"
        cached = _cached_record(cache_path, key)
        if cached is not None:
            for field in ("metrics", "result_path", "result_sha256", "peak_memory_bytes",
                          "evaluation_elapsed_seconds", "worker_elapsed_seconds"):
                record[field] = copy.deepcopy(cached.get(field))
            record.update({"status": "success", "cache_hit": True,
                           "source_attempt_id": cached["attempt_id"], "returncode": 0})
            record["elapsed_seconds"] = time.perf_counter() - started
            _atomic_json(record["record_path"], record)
            return record
        request = {
            "graph_path": str(graph_path), "plan_path": record["plan_path"],
            "config_path": str(config_path), "official_code": str(official_code),
            "problem": problem, "hashes": hashes,
            "result_path": str(attempt / "official_result.json.gz"),
            "worker_record_path": str(attempt / "worker.json"),
            "progress_path": str(attempt / "progress.json"),
        }
        request_path = attempt / "request.json"
        _atomic_json(request_path, request)
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
                    "PYTHONNOUSERSITE": "1"})
        with (attempt / "stdout.log").open("wb") as stdout, (attempt / "stderr.log").open("wb") as stderr:
            process = subprocess.Popen([sys.executable, "-B", "-s", str(worker), str(request_path)],
                                       cwd=str(attempt), env=env, stdout=stdout, stderr=stderr)
            try:
                process.wait(timeout=float(timeout))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                record.update({"status": "timeout", "returncode": process.returncode,
                               "error": "worker exceeded {:.6g} seconds".format(timeout)})
                try:
                    progress = _read_json(attempt / "progress.json")
                    record["peak_memory_bytes"] = progress.get("peak_memory_bytes")
                    record["metrics"]["peak_memory_bytes"] = progress.get("peak_memory_bytes")
                    record["rss_measurement"] = "sampled_before_timeout"
                    record["last_worker_progress"] = progress
                except (OSError, ValueError):
                    record["rss_measurement"] = "unavailable_before_timeout"
            else:
                record["returncode"] = process.returncode
                worker_record = _read_json(attempt / "worker.json")
                record.update({k: worker_record.get(k) for k in (
                    "status", "metrics", "evaluation_elapsed_seconds", "worker_elapsed_seconds",
                    "peak_memory_bytes", "error", "error_type", "error_stage", "rss_measurement")})
                if record["status"] == "success":
                    if process.returncode != 0:
                        raise RuntimeError("worker reported success with nonzero return code")
                    result_path = Path(request["result_path"])
                    if not result_path.is_file():
                        raise RuntimeError("worker success result is missing")
                    record.update({"result_path": str(result_path), "result_sha256": _sha256(result_path)})
        record["elapsed_seconds"] = time.perf_counter() - started
        _atomic_json(record["record_path"], record)
        if record["status"] == "success":
            _atomic_json(cache_path, record)
        return record
    except Exception as error:
        if record["status"] not in ("invalid_plan", "timeout"):
            record["status"] = "runtime_error"
        record["error"] = "{}: {}".format(type(error).__name__, error)
        record["elapsed_seconds"] = time.perf_counter() - started
        if attempt is not None:
            try:
                with (attempt / "stderr.log").open("a", encoding="utf-8") as stream:
                    stream.write(traceback.format_exc())
                _atomic_json(attempt / "record.json", record)
            except OSError:
                pass
        return record
