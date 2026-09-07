"""Compare rule detector snapshots without Git or optional profiling packages.

Run with the deployment Python environment. Each variant/phase runs in a fresh,
serial subprocess. Timings cover detect_from_path (decode through synchronous
output completion); hashing, warmup, RSS sampling and tracemalloc are excluded.
The report directory must be new. No existing directory is deleted.
"""
from __future__ import annotations

import argparse
import ctypes
import functools
import gc
import hashlib
import importlib
import inspect
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import threading
import time
import tracemalloc
import weakref
from pathlib import Path


FULL_FILES = (
    "01_gray.bmp", "02_coarse_flat.bmp", "03_coarse_binary.bmp",
    "04_refine_density.bmp", "05_contour_mask.bmp", "06_overlay.bmp",
    "07_result.json",
)
PRODUCTION_FILES = FULL_FILES[4:]
VARIANTS = ("baseline-full", "candidate-full", "candidate-production",
            "baseline-none", "candidate-none")


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8")


def _code_manifest(root):
    files = {}
    for folder in ("vision", "workflow", "config"):
        for path in sorted((root / folder).rglob("*.py")):
            files[path.relative_to(root).as_posix()] = _file_digest(path)
    return {"root": str(root), "sha256": _digest(files), "files": files}


def _expected_files(variant):
    if variant.endswith("-none"):
        return ()
    return PRODUCTION_FILES if variant == "candidate-production" else FULL_FILES


def _artifacts(directory, expected):
    import cv2
    import numpy as np

    actual = sorted(p.name for p in directory.iterdir()) if directory.exists() else []
    if actual != sorted(expected):
        raise AssertionError(f"Artifact set mismatch in {directory}: {actual}; expected {list(expected)}")
    result = {}
    for name in expected:
        path = directory / name
        record = {"bytes": path.stat().st_size}
        if path.suffix == ".bmp":
            pixels = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            if pixels is None:
                raise AssertionError(f"Cannot decode output: {path}")
            record.update(shape=list(pixels.shape), dtype=str(pixels.dtype),
                          pixel_sha256=hashlib.sha256(memoryview(pixels)).hexdigest())
        else:
            record["json_sha256"] = _digest(json.loads(path.read_text(encoding="utf-8")))
        result[name] = record
    return result


@functools.lru_cache(maxsize=1)
def _windows_rss_api():
    """Bind once: ctypes globally caches pointer types and would retain new classes."""
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    return Counters, kernel, psapi


def _rss_bytes():
    """Current resident bytes, never a high-water mark substituted for current RSS."""
    if os.name == "nt":
        Counters, kernel, psapi = _windows_rss_api()
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(counters.WorkingSetSize)
    if sys.platform.startswith("linux"):
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    raise RuntimeError("Current RSS measurement supports Windows and Linux only; use --skip-memory elsewhere")


class _RssSampler:
    def __init__(self):
        self.peak = _rss_bytes()
        self.error = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self):
        try:
            while not self.stop.wait(0.01):
                self.peak = max(self.peak, _rss_bytes())
        except Exception as exc:
            self.error = exc

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()
        self.peak = max(self.peak, _rss_bytes())
        if self.error is not None:
            raise self.error


def _watch_arrays(pipeline):
    """Observe ROI/full-frame arrays without retaining them; memory phase only."""
    import numpy as np

    refs = []

    def visit(value):
        if isinstance(value, np.ndarray):
            refs.append(weakref.ref(value))
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, (tuple, list)):
            for item in value:
                visit(item)

    def wrap(function):
        def watched(*args, **kwargs):
            visit(args)
            result = function(*args, **kwargs)
            visit(result)
            return result
        return watched

    for name in ("load_gray_image", "detect_and_refine", "refine_contour_in_roi"):
        setattr(pipeline, name, wrap(getattr(pipeline, name)))
    return refs


def _environment(cv2, np):
    return {"python": sys.version, "executable": sys.executable,
            "platform": platform.platform(), "machine": platform.machine(),
            "processor": platform.processor(), "cpu_count": os.cpu_count(),
            "opencv": cv2.__version__, "numpy": np.__version__,
            "opencv_threads": cv2.getNumThreads(),
            "opencv_build_sha256": hashlib.sha256(cv2.getBuildInformation().encode()).hexdigest()}


def _worker(request_path):
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    root, report = Path(request["root"]), Path(request["report_dir"])
    sys.path.insert(0, str(root))
    os.chdir(root)
    import cv2
    import numpy as np

    pipeline = importlib.import_module("vision.vision.detect_pipeline")
    if Path(pipeline.__file__).resolve() != root / "vision" / "vision" / "detect_pipeline.py":
        raise AssertionError(f"Wrong source imported: {pipeline.__file__}")
    entrypoint_source = Path(inspect.getfile(pipeline.detect_from_path)).resolve()
    if entrypoint_source != root / "vision" / "vision" / "detect_pipeline.py":
        raise AssertionError(f"Wrong entrypoint imported: {entrypoint_source}")
    entrypoint = {"module": pipeline.detect_from_path.__module__,
                  "source_path": str(entrypoint_source),
                  "source_sha256": _file_digest(entrypoint_source)}
    if request["threads"] is not None:
        cv2.setNumThreads(request["threads"])
    variant, phase = request["variant"], request["phase"]
    expected = _expected_files(variant)
    kwargs = dict(request["kwargs"])
    if variant.startswith("candidate-"):
        kwargs["save_debug"] = variant == "candidate-full"
    refs = _watch_arrays(pipeline) if phase == "rss" else []
    environment = _environment(cv2, np)
    manifest = _code_manifest(root)
    records, signatures = [], {}
    iterations = request["warmup"] + request["repeat"]
    for cycle in range(iterations):
        for index, image in enumerate(request["images"]):
            output = report / "artifacts" / variant / f"image-{index:03d}"
            if expected:
                output.mkdir(parents=True, exist_ok=True)
            random.seed(request["seed"])
            np.random.seed(request["seed"])
            cv2.setRNGSeed(request["seed"])
            gc.collect()
            refs.clear()
            before = _rss_bytes() if phase == "rss" else None
            if phase == "trace":
                tracemalloc.start()
            if phase == "rss":
                with _RssSampler() as sampler:
                    result = pipeline.detect_from_path(image, out_dir=output if expected else None, **kwargs)
                elapsed = None
                peak = sampler.peak
            else:
                started = time.perf_counter()
                result = pipeline.detect_from_path(image, out_dir=output if expected else None, **kwargs)
                elapsed = time.perf_counter() - started if phase == "timing" else None
                peak = None
            # Take trace peak before verification/decode can add unrelated allocations.
            trace_peak = tracemalloc.get_traced_memory()[1] if phase == "trace" else None
            result_digest = _digest(result)
            # Preserve the full business dictionary for reviewing no-output mode,
            # outside detector output directories and outside measured intervals.
            if phase == "timing":
                result_path = report / "results" / variant / f"image-{index:03d}.json"
                result_path.parent.mkdir(parents=True, exist_ok=True)
                _write_json(result_path, result)
            del result
            gc.collect()
            after = _rss_bytes() if phase == "rss" else None
            alive = sum(ref() is not None for ref in refs)
            watched = len(refs)
            refs.clear()
            trace_retained = tracemalloc.get_traced_memory()[0] if phase == "trace" else None
            if phase == "trace":
                tracemalloc.stop()
            artifacts = _artifacts(output, expected)
            if expected and artifacts["07_result.json"]["json_sha256"] != result_digest:
                raise AssertionError(f"Returned JSON differs from saved JSON: {variant}, image {index}")
            signature = {"result_sha256": result_digest, "artifacts": artifacts}
            if index in signatures and signatures[index] != signature:
                raise AssertionError(f"Repeated result changed: {variant}, image {index}, cycle {cycle}")
            signatures[index] = signature
            records.append({"cycle": cycle, "warmup": cycle < request["warmup"],
                            "image_index": index, "seconds": elapsed,
                            "result_sha256": result_digest, "output_bytes": sum(v["bytes"] for v in artifacts.values()),
                            "rss_before": before, "rss_after_release": after, "rss_sampled_peak": peak,
                            "watched_array_references": watched, "live_array_references": alive,
                            "tracemalloc_peak": trace_peak, "tracemalloc_after_release": trace_retained})
            print(f"{variant} {phase} cycle={cycle + 1}/{iterations} image={index + 1}/{len(request['images'])}", flush=True)
    if manifest != _code_manifest(root):
        raise AssertionError(f"Source changed while running {variant}/{phase}")
    _write_json(request["result_path"], {"variant": variant, "phase": phase,
                "environment": environment, "code": manifest, "entrypoint": entrypoint,
                "signatures": signatures, "records": records})


def _stats(values):
    if not values:
        return None
    ordered = sorted(values)
    # Linear interpolation, explicitly defined for reproducible small samples.
    position = 0.95 * (len(ordered) - 1)
    lo, hi = int(position), min(int(position) + 1, len(ordered) - 1)
    return {"count": len(values), "median": statistics.median(values),
            "p95": ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo),
            "min": min(values), "max": max(values)}


def _trend(values):
    if not values:
        return None
    midpoint = (len(values) - 1) / 2
    denominator = sum((i - midpoint) ** 2 for i in range(len(values)))
    slope = sum((i - midpoint) * value for i, value in enumerate(values)) / denominator if denominator else 0.0
    return {"values": values, "first": values[0], "last": values[-1],
            "delta": values[-1] - values[0], "bytes_per_call_slope": slope}


def _compare(results):
    """Exact comparisons; no tolerance, ignored fields, or normalized paths."""
    errors = []
    timing = {r["variant"]: r for r in results if r["phase"] == "timing"}
    for candidate, baseline in (("candidate-full", "baseline-full"),
                                ("candidate-production", "baseline-full"),
                                ("candidate-none", "baseline-none")):
        for index, actual in timing[candidate]["signatures"].items():
            wanted = timing[baseline]["signatures"][index]
            if actual["result_sha256"] != wanted["result_sha256"]:
                errors.append(f"{candidate} image {index}: JSON differs from {baseline}")
            for name, item in actual["artifacts"].items():
                if item != wanted["artifacts"].get(name):
                    errors.append(f"{candidate} image {index}: {name} differs from {baseline}")
    for result in results:
        reference = timing[result["variant"]]
        if result["signatures"] != reference["signatures"]:
            errors.append(f"{result['variant']}/{result['phase']}: changed output across phases")
        if result["code"] != reference["code"]:
            errors.append(f"{result['variant']}: changed source across phases")
        if any(row["live_array_references"] for row in result["records"]):
            errors.append(f"{result['variant']}/{result['phase']}: image buffers retained after return")
        if result["environment"] != reference["environment"]:
            errors.append(f"{result['variant']}: environment changed across phases")
    for candidate in ("candidate-full", "candidate-production", "candidate-none"):
        if timing[candidate]["environment"] != timing["baseline-full"]["environment"]:
            errors.append(f"{candidate}: environment differs from baseline")
    return errors


def _run(args):
    roots = {"baseline": Path(args.baseline_root).resolve(), "candidate": Path(args.candidate_root).resolve()}
    images = [str(Path(image).resolve()) for image in args.image]
    for root in roots.values():
        if not (root / "vision" / "vision" / "detect_pipeline.py").is_file():
            raise ValueError(f"Detector source missing: {root}")
    for image in images:
        if not Path(image).is_file():
            raise ValueError(f"Input image missing: {image}")
    kwargs = json.loads(Path(args.kwargs_file).read_text(encoding="utf-8")) if args.kwargs_file else {}
    if not isinstance(kwargs, dict) or set(kwargs) & {"image_path", "out_dir", "save_debug"}:
        raise ValueError("--kwargs-file must contain a JSON object without image_path, out_dir, or save_debug")
    report = Path(args.report_dir).resolve()
    report.mkdir(parents=True, exist_ok=False)
    initial_code = {name: _code_manifest(root) for name, root in roots.items()}
    inputs = [{"path": image, "bytes": Path(image).stat().st_size, "sha256": _file_digest(image)} for image in images]
    baseline_manifest = roots["baseline"] / "baseline_manifest.json"
    if baseline_manifest.exists():
        _write_json(report / "baseline_manifest.json", json.loads(baseline_manifest.read_text(encoding="utf-8")))
    config = {"images": images, "report_dir": str(report), "warmup": args.warmup,
              "repeat": args.repeat, "memory_repeat": args.memory_repeat or args.repeat,
              "seed": args.seed, "threads": args.threads, "kwargs": kwargs,
              "skip_memory": args.skip_memory, "inputs": inputs, "code": initial_code}
    _write_json(report / "request.json", config)
    results, errors = [], []
    phases = ("timing",) if args.skip_memory else ("timing", "rss", "trace")
    try:
        for phase in phases:
            for variant in VARIANTS:
                name = f"{variant}-{phase}"
                request = dict(config, variant=variant, phase=phase,
                               root=str(roots[variant.split("-")[0]]),
                               repeat=args.repeat if phase == "timing" else config["memory_repeat"],
                               result_path=str(report / f"{name}.json"))
                request_path = report / f"{name}-request.json"
                _write_json(request_path, request)
                command = [sys.executable, "-B", str(Path(__file__).resolve()), "--worker-request", str(request_path)]
                completed = subprocess.run(command, cwd=request["root"], check=False,
                                           env=dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1"))
                if completed.returncode:
                    raise RuntimeError(f"{name} failed with exit code {completed.returncode}; review preceding traceback")
                results.append(json.loads(Path(request["result_path"]).read_text(encoding="utf-8")))
        errors.extend(_compare(results))
        for name, root in roots.items():
            if initial_code[name] != _code_manifest(root):
                errors.append(f"{name} source changed during benchmark")
        for item in inputs:
            if item["sha256"] != _file_digest(item["path"]):
                errors.append(f"Input changed during benchmark: {item['path']}")
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    summary = {}
    for result in results:
        variant = result["variant"]
        rows = [row for row in result["records"] if not row["warmup"]]
        item = summary.setdefault(variant, {})
        if result["phase"] == "timing":
            item["seconds"] = _stats([row["seconds"] for row in rows])
            item["seconds_by_image"] = {str(index): _stats([row["seconds"] for row in rows if row["image_index"] == index]) for index in range(len(images))}
            item["output_bytes_per_image"] = {index: sum(v["bytes"] for v in signature["artifacts"].values()) for index, signature in result["signatures"].items()}
        elif result["phase"] == "rss":
            item["rss_sampled_peak_bytes"] = max(row["rss_sampled_peak"] for row in rows)
            item["rss_after_release_trend"] = _trend([row["rss_after_release"] for row in rows])
            item["retained_array_references"] = sum(row["live_array_references"] for row in rows)
        else:
            item["tracemalloc_peak_bytes"] = max(row["tracemalloc_peak"] for row in rows)
            item["tracemalloc_after_release_trend"] = _trend([row["tracemalloc_after_release"] for row in rows])
    final = {"passed": not errors, "errors": errors, "config": config,
             "summary": summary, "workers": results,
             "limits": ["Timing is detect_from_path through synchronous file API completion; it does not include workflow/API calls or guarantee physical-device cache flush.",
                        "Each phase and variant uses a separate serial process; report timestamps exclude profiling and output verification.",
                        "RSS is sampled every 10 ms and may miss shorter peaks; allocator caching can retain RSS without live arrays.",
                        "Finite repeats cannot prove absence of every leak. Review end-of-call RSS trends with a sufficiently long representative soak.",
                        "Tracemalloc is a separate phase and may not observe every native OpenCV allocation."]}
    _write_json(report / "report.json", final)
    lines = ["# Rule vision benchmark", "", f"Exact checks: {'PASS' if not errors else 'FAIL'}", "",
             "| Variant | Median seconds | P95 seconds | Output bytes per image |", "| --- | ---: | ---: | --- |"]
    for variant, item in summary.items():
        stats = item.get("seconds") or {}
        lines.append(f"| {variant} | {stats.get('median', '')} | {stats.get('p95', '')} | {item.get('output_bytes_per_image', {})} |")
    lines.extend(["", "## Interpretation", ""] + [f"- {value}" for value in final["limits"]])
    if errors:
        lines.extend(["", "## Errors", ""] + [f"- {error}" for error in errors])
    (report / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Report: {report / 'report.json'}; {'PASS' if not errors else 'FAIL'}", flush=True)
    return 1 if errors else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root")
    parser.add_argument("--candidate-root")
    parser.add_argument("--image", action="append", help="Input image; repeat to cover multiple images")
    parser.add_argument("--report-dir", help="New output directory; existing directories are refused")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--memory-repeat", type=int, help="RSS and trace measured cycles (default: --repeat)")
    parser.add_argument("--seed", type=int, default=20260905)
    parser.add_argument("--threads", type=int, help="OpenCV threads; omitted preserves runtime default")
    parser.add_argument("--kwargs-file", help="Optional JSON object of identical detector keyword arguments")
    parser.add_argument("--skip-memory", action="store_true")
    parser.add_argument("--worker-request", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker_request:
        _worker(args.worker_request)
        return 0
    if not all((args.baseline_root, args.candidate_root, args.image, args.report_dir)):
        parser.error("--baseline-root, --candidate-root, --image, and --report-dir are required")
    if args.warmup < 0 or args.repeat < 1 or (args.memory_repeat is not None and args.memory_repeat < 1):
        parser.error("warmup must be nonnegative; repeat and memory-repeat must be positive")
    if args.threads is not None and args.threads < 1:
        parser.error("threads must be positive")
    if not 0 <= args.seed <= 2147483647:
        parser.error("seed must fit a nonnegative signed 32-bit integer")
    try:
        return _run(args)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
