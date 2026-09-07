"""Acceptance checks must fail on missing artifacts or any business/pixel change."""
import copy
import ctypes
import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest


SOURCE = Path(__file__).resolve().parents[1] / "tools" / "benchmark_rule_vision.py"
SPEC = importlib.util.spec_from_file_location("benchmark_rule_vision", SOURCE)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def _results():
    records = []
    for variant in benchmark.VARIANTS:
        artifacts = {name: {"bytes": 100, "pixel_sha256": "same"}
                     for name in benchmark._expected_files(variant)}
        records.append({"variant": variant, "phase": "timing", "environment": {},
                        "code": {"root": variant.split("-")[0]},
                        "records": [{"live_array_references": 0}],
                        "signatures": {"0": {"result_sha256": "same", "artifacts": artifacts}}})
    return records


@pytest.mark.parametrize("change", ["json", "pixel", "retained", "environment", "cross_phase"])
def test_comparison_fails_on_changed_evidence(change):
    records = _results()
    assert benchmark._compare(records) == []
    candidate = records[2]
    if change == "json":
        candidate["signatures"]["0"]["result_sha256"] = "different"
    elif change == "pixel":
        candidate["signatures"]["0"]["artifacts"]["05_contour_mask.bmp"]["pixel_sha256"] = "different"
    elif change == "retained":
        candidate["records"][0]["live_array_references"] = 1
    elif change == "environment":
        candidate["environment"]["opencv_threads"] = 1
    else:
        traced = copy.deepcopy(candidate)
        traced["phase"] = "trace"
        traced["signatures"]["0"]["result_sha256"] = "different"
        records.append(traced)
    assert benchmark._compare(records)


def test_artifact_check_decodes_pixels_and_rejects_missing_or_extra(tmp_path):
    pixels = np.full((12, 13), 71, dtype=np.uint8)
    for name in benchmark.PRODUCTION_FILES[:-1]:
        cv2.imencode(".bmp", pixels)[1].tofile(str(tmp_path / name))
    (tmp_path / "07_result.json").write_text(json.dumps({"components": []}), encoding="utf-8")
    first = benchmark._artifacts(tmp_path, benchmark.PRODUCTION_FILES)
    assert first["05_contour_mask.bmp"]["shape"] == [12, 13]
    pixels[1, 2] = 72
    cv2.imencode(".bmp", pixels)[1].tofile(str(tmp_path / "05_contour_mask.bmp"))
    second = benchmark._artifacts(tmp_path, benchmark.PRODUCTION_FILES)
    assert first["05_contour_mask.bmp"]["pixel_sha256"] != second["05_contour_mask.bmp"]["pixel_sha256"]
    extra = tmp_path / "01_gray.bmp"
    extra.write_bytes(b"unexpected")
    with pytest.raises(AssertionError, match="Artifact set mismatch"):
        benchmark._artifacts(tmp_path, benchmark.PRODUCTION_FILES)
    extra.unlink()
    (tmp_path / "07_result.json").unlink()
    with pytest.raises(AssertionError, match="Artifact set mismatch"):
        benchmark._artifacts(tmp_path, benchmark.PRODUCTION_FILES)


def test_digest_does_not_ignore_scale_bar_or_numeric_types():
    assert benchmark._digest({"scale_bar": None}) != benchmark._digest({"scale_bar": {"length_px": 100}})
    assert benchmark._digest({"x": 1}) != benchmark._digest({"x": 1.0})


def test_existing_report_directory_is_refused_without_changes(tmp_path):
    snapshot = tmp_path / "snapshot"
    source = snapshot / "vision" / "vision" / "detect_pipeline.py"
    source.parent.mkdir(parents=True)
    source.write_text("# untouched", encoding="utf-8")
    image = tmp_path / "input.bmp"
    image.write_bytes(b"input")
    report = tmp_path / "existing-report"
    report.mkdir()
    keep = report / "keep.txt"
    keep.write_text("must survive", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        benchmark.main(["--baseline-root", str(snapshot), "--candidate-root", str(snapshot),
                        "--image", str(image), "--report-dir", str(report), "--skip-memory"])
    assert error.value.code == 2
    assert list(report.iterdir()) == [keep]
    assert keep.read_text(encoding="utf-8") == "must survive"


def test_rss_reports_current_bytes():
    assert benchmark._rss_bytes() > 0


def test_windows_rss_sampling_does_not_grow_ctypes_pointer_cache(monkeypatch):
    expected_rss = 64 * 1024 * 1024
    bindings = []

    def get_current_process():
        return 12345

    def get_process_memory_info(process, counters, size):
        assert process == 12345
        assert size == ctypes.sizeof(counters._obj)
        counters._obj.WorkingSetSize = expected_rss
        return True

    def win_dll(name, **kwargs):
        assert kwargs == {"use_last_error": True}
        bindings.append(name)
        return {
            "kernel32": SimpleNamespace(GetCurrentProcess=get_current_process),
            "psapi": SimpleNamespace(GetProcessMemoryInfo=get_process_memory_info),
        }[name]

    # Exercise the actual Windows type/binding path on Linux too, without
    # modifying the process-wide os.name used by pathlib and pytest.
    monkeypatch.setattr(benchmark, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", win_dll, raising=False)
    benchmark._windows_rss_api.cache_clear()
    try:
        assert benchmark._rss_bytes() == expected_rss
        initial_types = set(ctypes._pointer_type_cache)
        binding = benchmark._windows_rss_api()
        for _ in range(1000):
            assert benchmark._rss_bytes() == expected_rss
        assert benchmark._windows_rss_api() is binding
        assert set(ctypes._pointer_type_cache) == initial_types
        assert bindings == ["kernel32", "psapi"]
    finally:
        benchmark._windows_rss_api.cache_clear()


def test_rss_sampler_joins_threads_after_repeated_normal_and_exceptional_exits(monkeypatch):
    sampled = threading.Event()
    original_rss = benchmark._rss_bytes
    owner = threading.current_thread()

    def observe_sample():
        value = original_rss()
        if threading.current_thread() is not owner:
            sampled.set()
        return value

    monkeypatch.setattr(benchmark, "_rss_bytes", observe_sample)
    samplers = []
    for iteration in range(6):
        sampled.clear()
        sampler = benchmark._RssSampler()
        samplers.append(sampler)
        if iteration % 2:
            with pytest.raises(RuntimeError, match="test body failure"):
                with sampler:
                    assert sampled.wait(timeout=1), "The sampling thread must actually run."
                    raise RuntimeError("test body failure")
        else:
            with sampler:
                assert sampled.wait(timeout=1), "The sampling thread must actually run."
        assert sampler.stop.is_set()
        assert not sampler.thread.is_alive()
        assert sampler.thread not in threading.enumerate()
        assert sampler.peak > 0
    assert all(not sampler.thread.is_alive() for sampler in samplers)
