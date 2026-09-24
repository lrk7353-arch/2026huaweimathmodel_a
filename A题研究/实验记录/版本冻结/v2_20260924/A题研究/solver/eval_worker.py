"""One official evaluation per process. Invoked only by evaluator.py."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback

sys.dont_write_bytecode = True


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(path.parent),
                                         prefix=".tmp-", delete=False) as stream:
            temp = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.flush()
        os.replace(str(temp), str(path))
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def _peak_rss():
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(rss if sys.platform == "darwin" else rss * 1024)
    except (ImportError, AttributeError):
        return None


def _classify(error, stage):
    message = str(error).lower()
    name = type(error).__name__
    if stage not in ("load_plan", "evaluate"):
        return "runtime_error"
    if any(token in message for token in ("dependency cycle", "contains a cycle", "introduced a cycle",
                                          "deadlock", "made no progress")):
        return "execution_cycle"
    if name in ("Step2SchedulingError", "Step3SchedulingError") and any(
            token in message for token in ("capacity", "spill victim", "alloc peak")):
        return "capacity_failure"
    if stage == "load_plan" or name == "MulticoreCutError":
        return "invalid_plan"
    if "subgraph priority order violates" in message or "core schedule" in message:
        return "invalid_plan"
    return "runtime_error"


def _save_result(path, result):
    path = Path(path)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json.gz")
    os.close(handle)
    try:
        with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as stream:
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _verify_inputs(request):
    hashes = request["hashes"]
    files = [(request["graph_path"], hashes["graph_sha256"]),
             (request["config_path"], hashes["config_sha256"]),
             (__file__, hashes["worker_sha256"]),
             (Path(__file__).with_name("evaluator.py"), hashes["wrapper_sha256"])]
    code = Path(request["official_code"])
    observed_names = sorted(p.name for p in code.glob("*.py"))
    if observed_names != sorted(hashes["official_py_sha256"]):
        raise RuntimeError("official module set changed after hashing")
    files.extend((code / name, expected) for name, expected in hashes["official_py_sha256"].items())
    if request["plan_path"] is not None:
        files.append((request["plan_path"], hashes["plan_sha256"]))
    for path, expected in files:
        if _sha256(path) != expected:
            raise RuntimeError("input changed after hashing: " + str(path))


def run(request):
    started = time.perf_counter()
    state = {"status": "runtime_error", "metrics": {}, "error": None,
             "evaluation_elapsed_seconds": None, "error_stage": None, "error_type": None}
    stage = "verify_inputs"
    stop = threading.Event()

    def progress():
        while not stop.is_set():
            try:
                _atomic_json(request["progress_path"], {"stage": stage,
                             "worker_elapsed_seconds": time.perf_counter() - started,
                             "peak_memory_bytes": _peak_rss()})
            except OSError:
                pass
            stop.wait(0.2)

    reporter = threading.Thread(target=progress, daemon=True)
    reporter.start()
    evaluation_started = None
    try:
        _verify_inputs(request)
        sys.path.insert(0, request["official_code"])
        from contest_io import _read_json
        from evaluation_validation import read_evaluation_config
        stage = "load_config"
        settings = read_evaluation_config(request["config_path"])
        stage = "load_graph"
        graph = _read_json(request["graph_path"])
        stage = "load_plan"
        plan = (_read_json(request["plan_path"]) if request["plan_path"] is not None else None)
        problem = request["problem"]
        kwargs = {"bandwidth": settings["bandwidth"], "capacity": settings["capacity"]}
        stage = "load_config"
        if problem in (0, 1):
            from multicore_cut_evaluate_problem_1 import evaluate_scene_a, read_scene_a_config
            scene = read_scene_a_config(request["config_path"])
            kwargs.update(cross_core_wait=scene["task_cross_core_wait_cycles"],
                          same_core_wait=scene["task_same_core_wait_cycles"])
            if problem == 0:
                from singlecore_evaluate import evaluate_singlecore
                function, args = evaluate_singlecore, (graph,)
            else:
                function, args = evaluate_scene_a, (graph, plan)
        elif problem == 2:
            from multicore_cut_evaluate_problem_2 import evaluate_scene_b, read_scene_b_config
            scene = read_scene_b_config(request["config_path"])
            kwargs["cross_core_copy_delay"] = scene["cross_core_copy_delay_cycles"]
            function, args = evaluate_scene_b, (graph, plan)
        elif problem == 3:
            from multicore_cut_evaluate_problem_3 import evaluate_problem_3, read_scene_b_config, read_cache_config
            scene = read_scene_b_config(request["config_path"])
            kwargs.update(cross_core_copy_delay=scene["cross_core_copy_delay_cycles"],
                          **read_cache_config(request["config_path"]))
            function, args = evaluate_problem_3, (graph, plan)
        else:
            raise ValueError("unsupported problem")
        stage = "evaluate"
        evaluation_started = time.perf_counter()
        result = function(*args, **kwargs)
        state["evaluation_elapsed_seconds"] = time.perf_counter() - evaluation_started
        stage = "verify_after_evaluation"
        _verify_inputs(request)
        # Add the same provenance names the official CLI adds; leave model outputs intact.
        result["input_graph"] = Path(request["graph_path"]).name
        if problem != 0:
            result["input_plan"] = Path(request["plan_path"]).name
        stage = "save_result"
        _save_result(request["result_path"], result)
        metrics = {name: result[name] for name in (
            "makespan", "num_cores", "data_movement_bytes", "cache_stats", "memory_peak_by_core",
            "cross_task_traffic", "task_count", "capacity_bytes", "bandwidth_bytes_per_cycle",
            "cache_capacity_bytes", "cache_bandwidth_bytes_per_cycle") if name in result}
        metrics["active_cores"] = (int(any(op.get("op") not in ("COPY_IN", "COPY_OUT")
                                           for op in graph.get("ops", []))) if problem == 0
                                   else sum(bool(order) for order in plan["core_schedules"]))
        state.update(status="success", metrics=metrics)
    except Exception as error:
        if evaluation_started is not None and state["evaluation_elapsed_seconds"] is None:
            state["evaluation_elapsed_seconds"] = time.perf_counter() - evaluation_started
        state.update(status=_classify(error, stage), error="{}: {}".format(type(error).__name__, error),
                     error_type=type(error).__name__, error_stage=stage)
        traceback.print_exc(file=sys.stderr)
    finally:
        stop.set()
        reporter.join(timeout=1)
        state["worker_elapsed_seconds"] = time.perf_counter() - started
        state["peak_memory_bytes"] = _peak_rss()
        state["metrics"]["peak_memory_bytes"] = state["peak_memory_bytes"]
        state["rss_measurement"] = "worker_ru_maxrss" if state["peak_memory_bytes"] is not None else "unavailable"
        _atomic_json(request["worker_record_path"], state)
    return 0 if state["status"] == "success" else 1


if __name__ == "__main__":
    with Path(sys.argv[1]).open(encoding="utf-8") as stream:
        request_data = json.load(stream)
    raise SystemExit(run(request_data))
