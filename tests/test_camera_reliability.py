"""Reliability contracts for the shared Hikvision recording camera.

The tests deliberately use only the Python standard library and fake cameras.
They exercise lifecycle/concurrency state without requiring the MVS SDK, a
physical camera, Pillow, numpy, pydantic, or pytest.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEVICES_DIR = PROJECT_ROOT / "devices"
for _path in (str(PROJECT_ROOT), str(DEVICES_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def _install_optional_dependency_stubs() -> None:
    """Allow importing the controller in a dependency-light test runtime."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        pil_module = types.ModuleType("PIL")
        image_module = types.ModuleType("PIL.Image")
        image_module.fromarray = lambda value: value  # type: ignore[attr-defined]
        pil_module.Image = image_module  # type: ignore[attr-defined]
        sys.modules.setdefault("PIL", pil_module)
        sys.modules.setdefault("PIL.Image", image_module)

    try:
        import numpy  # noqa: F401
    except ImportError:
        sys.modules.setdefault("numpy", types.ModuleType("numpy"))


_install_optional_dependency_stubs()

import camera_controller as camera_module  # noqa: E402


def _video_info(path: str = "record.part.avi") -> camera_module.VideoRecordInfo:
    return camera_module.VideoRecordInfo(
        saved_path=path,
        width=5120,
        height=5120,
        pixel_type=0x01080001,
        frame_rate=10.0,
        bitrate_kbps=1000,
        frame_count=3,
        duration_s=0.3,
        timestamp_started=1.0,
        timestamp_finished=1.3,
    )


def _join(test: unittest.TestCase, thread: threading.Thread, timeout: float = 1.0) -> None:
    thread.join(timeout)
    test.assertFalse(thread.is_alive(), f"thread {thread.name!r} did not finish within {timeout}s")


class CameraControllerReliabilityTests(unittest.TestCase):
    def test_recording_status_does_not_wait_for_sdk_lock(self) -> None:
        cam = camera_module.HikCameraController()
        cam.opened = True
        cam.recording = True
        cam._record_frame_count = 7
        lock_held = threading.Event()
        release_lock = threading.Event()

        def hold_sdk_lock() -> None:
            with cam._sdk_lock:
                lock_held.set()
                release_lock.wait(1.0)

        holder = threading.Thread(target=hold_sdk_lock, name="test-sdk-lock-holder", daemon=True)
        holder.start()
        self.assertTrue(lock_held.wait(0.5), "test holder never acquired the SDK lock")
        try:
            started = time.monotonic()
            status = cam.recording_status()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.05, "status endpoint must not queue behind a native SDK call")
            self.assertTrue(status["recording"])
            self.assertTrue(status["opened"])
            self.assertFalse(status["sdk_hung"])
            self.assertEqual(status["frame_count"], 7)
        finally:
            release_lock.set()
            _join(self, holder)

    def test_native_timeout_marks_sdk_hung_and_rejects_reuse(self) -> None:
        cam = camera_module.HikCameraController()
        release_native_call = threading.Event()
        second_call_ran = threading.Event()

        try:
            started = time.monotonic()
            with self.assertRaises(camera_module.CameraSDKError) as timeout_error:
                cam._timed_sdk_call(
                    lambda: release_native_call.wait(1.0),
                    timeout_s=0.01,
                    name="fake-hung-call",
                )
            first_elapsed = time.monotonic() - started
            self.assertLess(first_elapsed, 0.35)
            self.assertIn("timed out", str(timeout_error.exception))
            self.assertTrue(cam._sdk_hung)

            started = time.monotonic()
            with self.assertRaises(camera_module.CameraSDKError) as reuse_error:
                cam._timed_sdk_call(
                    lambda: second_call_ran.set(),
                    timeout_s=1.0,
                    name="must-not-run",
                )
            reuse_elapsed = time.monotonic() - started
            self.assertLess(reuse_elapsed, 0.05)
            self.assertFalse(second_call_ran.is_set())
            self.assertIn("previous", str(reuse_error.exception))
        finally:
            release_native_call.set()

    def test_record_grab_timeout_boundaries_include_exposure_slack(self) -> None:
        resolve = camera_module.resolve_record_grab_timeout_ms
        self.assertEqual(resolve(None), camera_module.RECORD_GRAB_TRANSFER_SLACK_MS)
        self.assertEqual(resolve(None, exposure_us=5_000), 2_005)
        self.assertEqual(resolve(2_005, exposure_us=5_000), 2_005)
        self.assertEqual(resolve(None, exposure_us=10_000_000), 12_000)
        self.assertEqual(
            resolve(camera_module.RECORD_GRAB_TIMEOUT_MAX_MS),
            camera_module.RECORD_GRAB_TIMEOUT_MAX_MS,
        )
        with self.assertRaises(camera_module.CameraSDKError):
            resolve(2_004, exposure_us=5_000)
        with self.assertRaises(camera_module.CameraSDKError):
            resolve(camera_module.RECORD_GRAB_TIMEOUT_MAX_MS + 1)
        with self.assertRaises(camera_module.CameraSDKError):
            resolve(None, exposure_us=20_000_000)
        with self.assertRaises(camera_module.CameraSDKError):
            resolve(None, exposure_us=float("nan"))

    def test_open_close_open_close_runs_cleanup_for_both_cycles(self) -> None:
        class LifecycleProbe(camera_module.HikCameraController):
            def __init__(self) -> None:
                super().__init__()
                self.open_calls = 0
                self.close_calls = 0

            def _open_unlocked(self) -> None:
                if self.opened:
                    return
                self.open_calls += 1
                self.opened = True

            def _close_unlocked(self) -> bool:
                self.close_calls += 1
                self.opened = False
                self._closed = True
                return True

        cam = LifecycleProbe()
        cam.open()
        self.assertTrue(cam.close())
        cam.open()
        self.assertTrue(cam.close())
        self.assertEqual(cam.open_calls, 2)
        self.assertEqual(cam.close_calls, 2, "reopening must clear the closed sentinel")
        self.assertFalse(cam.opened)

    def test_sdk_finalize_failure_taints_controller_instead_of_reporting_closed(self) -> None:
        cam = camera_module.HikCameraController()
        cam._sdk_loaded = True
        cam._sdk_initialized = True
        cam._sdk = {"MvCamera": object()}
        original_release = camera_module._release_mvs_sdk
        camera_module._release_mvs_sdk = lambda _camera: False
        try:
            self.assertFalse(cam.close())
        finally:
            camera_module._release_mvs_sdk = original_release
        self.assertTrue(cam._sdk_hung)
        self.assertFalse(cam.close(), "a controller with uncertain SDK lifecycle must never be reused")

    def test_slow_frames_still_yield_between_sdk_lock_acquisitions(self) -> None:
        class CountingStopEvent:
            def __init__(self) -> None:
                self.event = threading.Event()
                self.wait_timeouts: list[float] = []

            def is_set(self) -> bool:
                return self.event.is_set()

            def set(self) -> None:
                self.event.set()

            def wait(self, timeout: float | None = None) -> bool:
                self.wait_timeouts.append(float(timeout or 0.0))
                return self.event.wait(timeout)

        cam = camera_module.HikCameraController()
        stop_event = CountingStopEvent()
        cam._record_stop_event = stop_event  # type: ignore[assignment]
        cam._record_frame_rate = 50.0
        frames = 0

        def grab_slow_frame(*, timeout_ms: int | None = None) -> tuple[object, object, int]:
            nonlocal frames
            time.sleep(0.03)  # slower than the 20 ms target interval
            frames += 1
            if frames >= 3:
                stop_event.set()
            return object(), object(), 1

        cam._grab_encode_record_frame_unlocked = grab_slow_frame  # type: ignore[method-assign]
        cam._fulfill_snapshot_requests = lambda data, info: None  # type: ignore[method-assign]
        cam._recording_worker(timeout_ms=2_000)

        self.assertEqual(frames, 3)
        self.assertTrue(
            any(timeout > 0 for timeout in stop_event.wait_timeouts),
            "a slow camera must still receive a scheduled wait/yield before the next frame",
        )

    def test_stop_rejects_new_recording_snapshots_immediately(self) -> None:
        cam = camera_module.HikCameraController()
        worker_release = threading.Event()
        worker_started = threading.Event()

        def blocked_worker() -> None:
            worker_started.set()
            worker_release.wait(1.0)

        worker = threading.Thread(target=blocked_worker, name="test-blocked-record-worker", daemon=True)
        cam.recording = True
        cam._record_snapshot_accepting = True
        cam._record_thread = worker
        cam._join_timeout_for_recording_stop = lambda value: 0.25  # type: ignore[method-assign]
        cam._stop_recording_unlocked = lambda: _video_info()  # type: ignore[method-assign]
        worker.start()
        self.assertTrue(worker_started.wait(0.5))

        stop_errors: list[BaseException] = []

        def stop_camera() -> None:
            try:
                cam.stop_background_recording()
            except BaseException as exc:  # retained for an assertion in the owning thread
                stop_errors.append(exc)

        stopper = threading.Thread(target=stop_camera, name="test-camera-stopper", daemon=True)
        stopper.start()
        self.assertTrue(cam._record_stop_event.wait(0.5), "stop did not publish its stop request")
        # Give stop_background_recording a scheduling turn to close snapshot admission.
        time.sleep(0.01)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                started = time.monotonic()
                with self.assertRaises(camera_module.CameraSDKError):
                    cam.capture_snapshot_during_recording(
                        str(Path(temp_dir) / "must-not-queue.bmp"),
                        timeout_ms=100,
                    )
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 0.05, "snapshot rejection must not wait for worker shutdown")
        finally:
            worker_release.set()
            _join(self, worker)
            _join(self, stopper)
        self.assertEqual(stop_errors, [])

    def test_background_failure_is_not_promoted_as_a_successful_stop(self) -> None:
        cam = camera_module.HikCameraController()
        cam.recording = True
        cam._record_error = "injected frame acquisition failure"
        stop_calls = 0

        def finalize_native_recording() -> camera_module.VideoRecordInfo:
            nonlocal stop_calls
            stop_calls += 1
            cam.recording = False
            return _video_info()

        cam._stop_recording_unlocked = finalize_native_recording  # type: ignore[method-assign]
        with self.assertRaises(camera_module.CameraSDKError) as caught:
            cam.stop_background_recording()
        self.assertEqual(stop_calls, 1, "the temporary AVI should still be finalized for diagnostics")
        self.assertIn("frame acquisition failure", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
