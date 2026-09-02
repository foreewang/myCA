"""Fault-injection tests for the camera process boundary.

These tests use a spawned fake worker and the standard library only.  A hang
is a real process-level hang, so the tests prove that the API-side supervisor
can terminate it and continue with a fresh PID.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from workflow.camera_process_supervisor import (  # noqa: E402
    CameraProcessError,
    CameraProcessSupervisor,
    CameraProcessTimeout,
    CameraStateError,
    CameraWorkerError,
    _camera_settings_match,
)
import workflow.camera_process_supervisor as supervisor_module  # noqa: E402


def _fake_worker(connection) -> None:
    session_id = None
    active_settings: dict[str, Any] = {}
    recording = False
    recording_started_at = 0.0
    opened = False
    frame_count = 0
    connection.send({"kind": "ready", "pid": os.getpid()})
    try:
        while True:
            try:
                request = connection.recv()
            except EOFError:
                return
            request_id = str(request.get("id") or "")
            command = str(request.get("command") or "")
            payload = dict(request.get("payload") or {})

            if command == "hang":
                while True:
                    time.sleep(1.0)

            if command == "exit":
                os._exit(17)

            if command in {"open", "start_recording"}:
                settings = dict(payload.get("settings") or {})
                if settings.get("_hang_start"):
                    while True:
                        time.sleep(1.0)
                delay = float(settings.get("_delay_s") or 0.0)
                if delay:
                    time.sleep(delay)
                if settings.get("_fail"):
                    connection.send(
                        {
                            "id": request_id,
                            "ok": False,
                            "error_type": "LookupError",
                            "message": "injected constructor failure",
                            "traceback": "injected",
                            "fatal": False,
                        }
                    )
                    continue
                opened = True
                recording = command == "start_recording"
                if recording:
                    recording_started_at = time.monotonic()
                    frame_count = 0
                session_id = str(payload.get("session_id") or "")
                active_settings = settings
                connection.send(
                    {
                        "id": request_id,
                        "ok": True,
                        "result": {
                            "session_id": session_id,
                            "status": {
                                "opened": True,
                                "recording": recording,
                                "background": recording,
                                "frame_count": frame_count,
                            },
                        },
                    }
                )
                continue

            if command == "status":
                failed_background = bool(
                    recording
                    and active_settings.get("_record_failure_after_s") is not None
                    and time.monotonic() - recording_started_at
                    >= float(active_settings["_record_failure_after_s"])
                )
                connection.send(
                    {
                        "id": request_id,
                        "ok": True,
                        "result": {
                            "opened": opened,
                            "recording": recording,
                            "background": recording and not failed_background,
                            "error": "injected background recording failure" if failed_background else None,
                            "frame_count": frame_count,
                            "session_id": session_id,
                            "settings": active_settings,
                        },
                    }
                )
                continue

            if command == "capture":
                if str(payload.get("session_id") or "") != str(session_id or ""):
                    connection.send(
                        {
                            "id": request_id,
                            "ok": False,
                            "error_type": "RuntimeError",
                            "message": "stale session",
                            "traceback": "",
                            "fatal": False,
                        }
                    )
                    continue
                frame_count += 1
                connection.send(
                    {
                        "id": request_id,
                        "ok": True,
                        "result": {
                            "frame": {
                                "width": 32,
                                "height": 24,
                                "frame_num": frame_count,
                                "pixel_type": 0x01080001,
                                "frame_len": 768,
                                "saved_path": str(payload.get("save_path") or ""),
                                "timestamp": time.time(),
                            },
                            "status": {
                                "opened": opened,
                                "recording": recording,
                                "background": recording,
                                "frame_count": frame_count,
                            },
                        },
                    }
                )
                continue

            if command == "stop_recording":
                if active_settings.get("_hang_stop"):
                    while True:
                        time.sleep(1.0)
                delay = float(active_settings.get("_stop_delay_s") or 0.0)
                if delay:
                    time.sleep(delay)
                part_path = str(active_settings.get("recording_path") or "record.part.avi")
                if active_settings.get("_write_part"):
                    Path(part_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(part_path).write_bytes(b"fake-avi")
                recording = False
                opened = False
                connection.send(
                    {
                        "id": request_id,
                        "ok": True,
                        "result": {
                            "video": {
                                "saved_path": str(active_settings.get("_returned_path") or part_path),
                                "width": 32,
                                "height": 24,
                                "pixel_type": 0x01080001,
                                "frame_rate": 10.0,
                                "bitrate_kbps": 1000,
                                "frame_count": (
                                    0
                                    if active_settings.get("_zero_frames")
                                    else max(frame_count, 1)
                                ),
                                "duration_s": 0.2,
                                "timestamp_started": 1.0,
                                "timestamp_finished": 1.2,
                            }
                        },
                    }
                )
                session_id = None
                active_settings = {}
                continue

            if command == "close":
                if active_settings.get("_fail_close"):
                    connection.send(
                        {
                            "id": request_id,
                            "ok": False,
                            "error_type": "RuntimeError",
                            "message": "injected close failure",
                            "traceback": "injected",
                            "fatal": True,
                        }
                    )
                    continue
                opened = False
                session_id = None
                active_settings = {}
                connection.send({"id": request_id, "ok": True, "result": {"closed": True}})
                continue

            if command in {"set_exposure", "set_gain"}:
                if str(payload.get("session_id") or "") != str(session_id or ""):
                    connection.send(
                        {
                            "id": request_id,
                            "ok": False,
                            "error_type": "RuntimeError",
                            "message": "stale session",
                            "traceback": "",
                            "fatal": False,
                        }
                    )
                    continue
                if command == "set_exposure":
                    active_settings["exposure_auto"] = False
                    active_settings["exposure_us"] = float(payload.get("value"))
                else:
                    active_settings["gain"] = float(payload.get("value"))
                connection.send({"id": request_id, "ok": True, "result": {"value": payload.get("value")}})
                continue

            if command == "ping":
                connection.send(
                    {"id": request_id, "ok": True, "result": {"pid": os.getpid(), "status": "ready"}}
                )
                continue

            if command == "shutdown":
                connection.send({"id": request_id, "ok": True, "result": {"closed": True}})
                return

            connection.send(
                {
                    "id": request_id,
                    "ok": False,
                    "error_type": "ValueError",
                    "message": f"unsupported fake command {command}",
                    "traceback": "",
                    "fatal": False,
                }
            )
    finally:
        connection.close()


class CameraProcessSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_open_timeout = supervisor_module.OPEN_COMMAND_TIMEOUT_S
        self.original_stop_timeout = supervisor_module.STOP_COMMAND_TIMEOUT_S
        self.original_stall_min = supervisor_module.RECORDING_STALL_MIN_S
        self.original_stall_grace = supervisor_module.RECORDING_STALL_GRACE_S
        self.temp_dir = tempfile.TemporaryDirectory()
        self.supervisor = CameraProcessSupervisor(
            worker_target=_fake_worker,
            monitor_interval_s=60.0,
        )

    def tearDown(self) -> None:
        self.supervisor.shutdown(timeout_s=1.0)
        supervisor_module.OPEN_COMMAND_TIMEOUT_S = self.original_open_timeout
        supervisor_module.STOP_COMMAND_TIMEOUT_S = self.original_stop_timeout
        supervisor_module.RECORDING_STALL_MIN_S = self.original_stall_min
        supervisor_module.RECORDING_STALL_GRACE_S = self.original_stall_grace
        self.temp_dir.cleanup()

    def test_timeout_environment_values_must_be_finite_and_bounded(self) -> None:
        for invalid in ("nan", "inf", "0", "301", "not-a-number"):
            with self.subTest(value=invalid), mock.patch.dict(
                os.environ, {"TEST_CAMERA_TIMEOUT_S": invalid}
            ):
                with self.assertRaises(RuntimeError):
                    supervisor_module._env_seconds(
                        "TEST_CAMERA_TIMEOUT_S", 5.0, minimum=0.1, maximum=300.0
                    )

    def recording_payload(self, **settings: Any) -> dict[str, Any]:
        default_final = Path(self.temp_dir.name) / "record.avi"
        default_part = Path(self.temp_dir.name) / "record.session.part.avi"
        values = {
            "save_path": str(default_final),
            "recording_path": str(default_part),
            "device_index": 0,
            "pixel_format": "mono8",
            "_write_part": True,
            **settings,
        }
        return {
            "recording_path": str(values["recording_path"]),
            "settings": values,
            "fps": 10.0,
            "bitrate_kbps": 1000,
            "timeout_ms": 2000,
        }

    def test_real_process_hang_is_terminated_and_replaced(self) -> None:
        first = self.supervisor._request("ping", {}, 2.0)
        first_pid = int(first["pid"])
        started = time.monotonic()
        with self.assertRaises(CameraProcessTimeout):
            self.supervisor._request("hang", {}, 0.2)
        self.assertLess(time.monotonic() - started, 5.0)

        status = self.supervisor.status()
        second_pid = int(status["worker_pid"])
        self.assertNotEqual(first_pid, second_pid)
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["worker_restart_count"], 1)
        self.assertIn("timed out", str(status["error"]))
        self.assertEqual(int(self.supervisor._request("ping", {}, 2.0)["pid"]), second_pid)

    def test_production_worker_target_starts_with_spawn_context(self) -> None:
        production = CameraProcessSupervisor(monitor_interval_s=60.0)
        try:
            response = production._request("ping", {}, 2.0)
            self.assertEqual(response["status"], "ready")
            self.assertGreater(int(response["pid"]), 0)
            self.assertTrue(production._process.daemon)
        finally:
            production.shutdown(timeout_s=1.0)

    def test_lifespan_shutdown_gate_prevents_late_worker_recreation(self) -> None:
        supervisor_module.reset_camera_process_supervisor_for_tests()
        try:
            first = supervisor_module.initialize_camera_process_supervisor()
            supervisor_module.shutdown_camera_process_supervisor(timeout_s=0.1)
            with self.assertRaises(CameraStateError):
                supervisor_module.get_camera_process_supervisor()
            second = supervisor_module.initialize_camera_process_supervisor()
            self.assertIsNot(first, second)
        finally:
            supervisor_module.reset_camera_process_supervisor_for_tests()

    def test_start_failure_clears_starting_and_allows_retry(self) -> None:
        with self.assertRaises(CameraWorkerError):
            self.supervisor.start_recording(self.recording_payload(_fail=True))
        failed = self.supervisor.status()
        self.assertEqual(failed["state"], "idle")
        self.assertFalse(self.supervisor.is_busy())
        self.assertIn("constructor failure", str(failed["error"]))

        started = self.supervisor.start_recording(self.recording_payload())
        self.assertEqual(started["state"], "recording")
        self.assertTrue(started["recording"])
        self.assertIsNotNone(self.supervisor.stop_recording())

    def test_status_is_local_and_duplicate_start_is_rejected(self) -> None:
        outcome: dict[str, Any] = {}

        def start_slowly() -> None:
            try:
                outcome["result"] = self.supervisor.start_recording(
                    self.recording_payload(_delay_s=0.3)
                )
            except BaseException as exc:
                outcome["error"] = exc

        import threading

        thread = threading.Thread(target=start_slowly, daemon=True)
        thread.start()
        deadline = time.monotonic() + 1.0
        while self.supervisor.status()["state"] != "starting" and time.monotonic() < deadline:
            time.sleep(0.005)

        started = time.monotonic()
        status = self.supervisor.status()
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertEqual(status["state"], "starting")
        with self.assertRaises(CameraStateError):
            self.supervisor.start_recording(self.recording_payload())

        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["result"]["state"], "recording")
        self.supervisor.stop_recording()

    def test_stop_state_is_visible_and_duplicate_stop_is_rejected(self) -> None:
        self.supervisor.start_recording(self.recording_payload(_stop_delay_s=0.3))
        outcome: dict[str, Any] = {}

        def stop_slowly() -> None:
            try:
                outcome["result"] = self.supervisor.stop_recording()
            except BaseException as exc:
                outcome["error"] = exc

        import threading

        thread = threading.Thread(target=stop_slowly, daemon=True)
        thread.start()
        deadline = time.monotonic() + 1.0
        while self.supervisor.status()["state"] != "stopping" and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(self.supervisor.status()["state"], "stopping")
        with self.assertRaises(CameraStateError):
            self.supervisor.stop_recording()
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", outcome)
        self.assertEqual(self.supervisor.status()["state"], "idle")

    def test_ordinary_camera_proxy_and_recording_snapshot_share_process(self) -> None:
        settings = {"device_index": 0, "pixel_format": "mono8"}
        proxy = self.supervisor.open_camera(settings)
        frame = proxy.capture_once("first.bmp", timeout_ms=100)
        self.assertEqual(frame.frame_num, 1)
        self.assertTrue(proxy.close())
        self.assertEqual(self.supervisor.status()["state"], "idle")

        self.supervisor.start_recording(self.recording_payload())
        status = self.supervisor.status()
        recording_proxy = self.supervisor.open_camera(settings)
        self.assertTrue(recording_proxy.recording_shared)
        frame = recording_proxy.capture_once("recording.bmp", timeout_ms=100)
        self.assertEqual(frame.frame_num, 1)
        self.assertTrue(recording_proxy.close())
        self.assertEqual(self.supervisor.status()["session_id"], status["session_id"])
        self.supervisor.stop_recording()

    def test_async_start_and_stop_return_before_native_work_finishes(self) -> None:
        started = time.monotonic()
        accepted = self.supervisor.begin_start_recording(
            self.recording_payload(_delay_s=0.3, _stop_delay_s=0.3)
        )
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertEqual(accepted["state"], "starting")

        deadline = time.monotonic() + 2.0
        while self.supervisor.status()["state"] == "starting" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.supervisor.status()["state"], "recording")

        started = time.monotonic()
        accepted = self.supervisor.begin_stop_recording()
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertEqual(accepted["state"], "stopping")
        deadline = time.monotonic() + 2.0
        while self.supervisor.status()["state"] == "stopping" and time.monotonic() < deadline:
            time.sleep(0.01)
        stopped = self.supervisor.status()
        self.assertEqual(stopped["state"], "idle")
        self.assertEqual(
            stopped["last_video"]["saved_path"],
            str(Path(self.temp_dir.name) / "record.avi"),
        )

    def test_completed_part_file_is_atomically_promoted_to_final_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            final_path = Path(temp_dir) / "record.avi"
            part_path = Path(temp_dir) / "record.session.part.avi"
            payload = self.recording_payload(
                save_path=str(final_path),
                recording_path=str(part_path),
                _write_part=True,
            )
            payload["recording_path"] = str(part_path)
            self.supervisor.start_recording(payload)
            info = self.supervisor.stop_recording()
            self.assertEqual(info.saved_path, str(final_path))
            self.assertEqual(final_path.read_bytes(), b"fake-avi")
            self.assertFalse(part_path.exists())
            self.assertEqual(
                self.supervisor.status()["last_video"]["saved_path"],
                str(final_path),
            )

    def test_missing_part_file_is_not_reported_as_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            final_path = Path(temp_dir) / "record.avi"
            part_path = Path(temp_dir) / "record.session.part.avi"
            payload = self.recording_payload(
                save_path=str(final_path),
                recording_path=str(part_path),
                _write_part=False,
            )
            payload["recording_path"] = str(part_path)
            self.supervisor.start_recording(payload)
            with self.assertRaises(CameraWorkerError) as caught:
                self.supervisor.stop_recording()
            self.assertEqual(caught.exception.error_type, "CAMERA_RECORD_FILE_MISSING")
            self.assertFalse(final_path.exists())

    def test_unexpected_worker_video_path_is_not_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            final_path = Path(temp_dir) / "record.avi"
            part_path = Path(temp_dir) / "record.session.part.avi"
            wrong_path = Path(temp_dir) / "wrong.part.avi"
            wrong_path.write_bytes(b"do-not-move")
            payload = self.recording_payload(
                save_path=str(final_path),
                recording_path=str(part_path),
                _returned_path=str(wrong_path),
            )
            payload["recording_path"] = str(part_path)
            self.supervisor.start_recording(payload)
            with self.assertRaises(CameraWorkerError):
                self.supervisor.stop_recording()
            status = self.supervisor.status()
            self.assertEqual(status["state"], "idle")
            self.assertEqual(status["error_code"], "CAMERA_RECORD_PATH_MISMATCH")
            self.assertTrue(wrong_path.exists())
            self.assertFalse(final_path.exists())

    def test_zero_frame_recording_is_not_reported_as_success(self) -> None:
        self.supervisor.start_recording(self.recording_payload(_zero_frames=True))
        with self.assertRaises(CameraWorkerError) as caught:
            self.supervisor.stop_recording()
        self.assertEqual(caught.exception.error_type, "CAMERA_RECORD_EMPTY")
        status = self.supervisor.status()
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["error_code"], "CAMERA_RECORD_EMPTY")
        self.assertEqual(status["last_video"], {})

    def test_async_native_hang_recovers_without_api_process_restart(self) -> None:
        supervisor_module.OPEN_COMMAND_TIMEOUT_S = 0.2
        accepted = self.supervisor.begin_start_recording(
            self.recording_payload(_hang_start=True)
        )
        self.assertEqual(accepted["state"], "starting")
        deadline = time.monotonic() + 5.0
        while self.supervisor.status()["state"] != "idle" and time.monotonic() < deadline:
            time.sleep(0.01)
        recovered = self.supervisor.status()
        self.assertEqual(recovered["state"], "idle")
        self.assertEqual(recovered["worker_restart_count"], 1)
        self.assertIn("timed out", str(recovered["error"]))

        # The same API process can start a fresh recording after recovery.
        started = self.supervisor.start_recording(self.recording_payload())
        self.assertEqual(started["state"], "recording")
        self.supervisor.stop_recording()

    def test_async_stop_hang_is_quarantined_and_recovered(self) -> None:
        supervisor_module.STOP_COMMAND_TIMEOUT_S = 0.2
        self.supervisor.start_recording(self.recording_payload(_hang_stop=True))
        accepted = self.supervisor.begin_stop_recording()
        self.assertEqual(accepted["state"], "stopping")
        deadline = time.monotonic() + 5.0
        while self.supervisor.status()["state"] != "idle" and time.monotonic() < deadline:
            time.sleep(0.01)
        recovered = self.supervisor.status()
        self.assertEqual(recovered["state"], "idle")
        self.assertEqual(recovered["worker_restart_count"], 1)
        self.assertFalse(self.supervisor.is_recording_busy())

    def test_close_failure_quarantines_handle_and_next_open_succeeds(self) -> None:
        proxy = self.supervisor.open_camera(
            {"device_index": 0, "pixel_format": "mono8", "_fail_close": True}
        )
        with self.assertRaises(CameraWorkerError):
            proxy.close()
        failed = self.supervisor.status()
        self.assertEqual(failed["state"], "idle")
        self.assertEqual(failed["worker_restart_count"], 1)
        self.assertIn("close failure", str(failed["error"]))

        replacement = self.supervisor.open_camera(
            {"device_index": 0, "pixel_format": "mono8"}
        )
        self.assertTrue(replacement.close())

    def test_unexpected_worker_exit_invalidates_session_and_recovers(self) -> None:
        proxy = self.supervisor.open_camera({"device_index": 0, "pixel_format": "mono8"})
        with self.assertRaises(CameraProcessError):
            self.supervisor._request("exit", {}, 2.0)
        recovered = self.supervisor.status()
        self.assertEqual(recovered["state"], "idle")
        self.assertEqual(recovered["worker_restart_count"], 1)
        self.assertFalse(proxy.opened)

        replacement = self.supervisor.open_camera(
            {"device_index": 0, "pixel_format": "mono8"}
        )
        self.assertTrue(replacement.close())

    def test_monitor_quarantines_failed_background_recording(self) -> None:
        monitored = CameraProcessSupervisor(
            worker_target=_fake_worker,
            monitor_interval_s=0.05,
        )
        try:
            monitored.start_recording(
                self.recording_payload(_record_failure_after_s=0.05)
            )
            deadline = time.monotonic() + 5.0
            while monitored.status()["state"] != "idle" and time.monotonic() < deadline:
                time.sleep(0.01)
            status = monitored.status()
            self.assertEqual(status["state"], "idle")
            self.assertEqual(status["worker_restart_count"], 1)
            self.assertIn("background recording failure", str(status["error"]))
            self.assertFalse(monitored.is_recording_busy())
        finally:
            monitored.shutdown(timeout_s=1.0)

    def test_monitor_quarantines_recording_that_stops_producing_frames(self) -> None:
        supervisor_module.RECORDING_STALL_MIN_S = 0.15
        supervisor_module.RECORDING_STALL_GRACE_S = 0.05
        monitored = CameraProcessSupervisor(
            worker_target=_fake_worker,
            monitor_interval_s=0.05,
        )
        try:
            monitored.start_recording(
                self.recording_payload(fps=100.0, timeout_ms=1)
            )
            deadline = time.monotonic() + 5.0
            while monitored.status()["state"] != "idle" and time.monotonic() < deadline:
                time.sleep(0.01)
            status = monitored.status()
            self.assertEqual(status["state"], "idle")
            self.assertEqual(status["worker_restart_count"], 1)
            self.assertEqual(status["error_code"], "CAMERA_RECORDING_STALLED")
            self.assertIn("no new frame", str(status["error"]))
        finally:
            monitored.shutdown(timeout_s=1.0)

    def test_shutdown_terminates_a_command_that_owns_the_lock(self) -> None:
        import threading

        errors: list[BaseException] = []

        def run_hang() -> None:
            try:
                self.supervisor._request("hang", {}, 30.0)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run_hang, daemon=True)
        thread.start()
        deadline = time.monotonic() + 2.0
        while self.supervisor.status()["worker_pid"] is None and time.monotonic() < deadline:
            time.sleep(0.01)

        started = time.monotonic()
        self.supervisor.shutdown(timeout_s=0.2)
        elapsed = time.monotonic() - started
        thread.join(2.0)
        self.assertLess(elapsed, 3.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertEqual(self.supervisor.status()["state"], "idle")

    def test_graceful_shutdown_finalizes_active_recording(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            final_path = Path(temp_dir) / "shutdown.avi"
            part_path = Path(temp_dir) / "shutdown.session.part.avi"
            payload = self.recording_payload(
                save_path=str(final_path),
                recording_path=str(part_path),
                _write_part=True,
            )
            payload["recording_path"] = str(part_path)
            self.supervisor.start_recording(payload)
            self.supervisor.shutdown(timeout_s=2.0)
            self.assertEqual(final_path.read_bytes(), b"fake-avi")
            self.assertFalse(part_path.exists())
            self.assertEqual(
                self.supervisor.status()["last_video"]["saved_path"],
                str(final_path),
            )

    def test_settings_match_requires_explicit_exposure_and_gain_compatibility(self) -> None:
        active = {
            "serial_number": "DA8583237",
            "camera_ip": "192.168.1.253",
            "pixel_format": "Mono_8",
            "exposure_us": 5000.0,
            "gain": 0.0,
        }
        self.assertTrue(
            _camera_settings_match(
                active,
                {
                    "serial_number": "DA8583237",
                    "pixel_format": "mono8",
                    "exposure_us": 5000.000001,
                    "gain": 0,
                },
            )
        )
        self.assertTrue(
            _camera_settings_match(
                active,
                {
                    "serial_number": "DA8583237",
                    "pixel_format": "mono8",
                    "exposure_auto": False,
                    "exposure_us": 5000.0,
                },
            )
        )
        self.assertTrue(
            _camera_settings_match(
                active,
                {"serial_number": "DA8583237", "pixel_format": "mono8"},
            )
        )
        for requested in (
            {"exposure_us": 20000.0},
            {"gain": 1.0},
            {"exposure_auto": True},
        ):
            with self.subTest(requested=requested):
                self.assertFalse(
                    _camera_settings_match(
                        active,
                        {
                            "serial_number": "DA8583237",
                            "pixel_format": "mono8",
                            **requested,
                        },
                    )
                )

    def test_shutdown_cancels_reserved_start_before_any_native_command(self) -> None:
        context = self.supervisor._reserve_recording_start(self.recording_payload())
        self.assertEqual(self.supervisor.status()["state"], "starting")

        self.supervisor.shutdown(timeout_s=0.5)

        self.assertTrue(context.done_event.is_set())
        self.assertEqual(self.supervisor.status()["state"], "idle")
        self.assertIsNone(self.supervisor.status()["worker_pid"])
        with self.assertRaises(CameraStateError):
            self.supervisor._complete_recording_start(context)
        self.assertEqual(self.supervisor.status()["state"], "idle")

    def test_shutdown_takes_over_reserved_stop_and_promotes_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            final_path = Path(temp_dir) / "reserved-stop.avi"
            part_path = Path(temp_dir) / "reserved-stop.session.part.avi"
            payload = self.recording_payload(
                save_path=str(final_path),
                recording_path=str(part_path),
                _write_part=True,
            )
            payload["recording_path"] = str(part_path)
            self.supervisor.start_recording(payload)
            context = self.supervisor._reserve_recording_stop()
            commands: list[str] = []
            original_request = self.supervisor._request_locked

            def count_request(command: str, request_payload: Any, timeout_s: float) -> Any:
                commands.append(command)
                return original_request(command, request_payload, timeout_s)

            with mock.patch.object(self.supervisor, "_request_locked", side_effect=count_request):
                self.supervisor.shutdown(timeout_s=2.0)

            self.assertTrue(context.done_event.is_set())
            self.assertEqual(commands.count("stop_recording"), 1)
            self.assertEqual(final_path.read_bytes(), b"fake-avi")
            self.assertFalse(part_path.exists())
            status = self.supervisor.status()
            self.assertEqual(status["state"], "idle")
            self.assertEqual(status["last_video"]["saved_path"], str(final_path))

    def test_shutdown_after_start_response_waits_for_commit_then_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            final_path = Path(temp_dir) / "start-commit.avi"
            part_path = Path(temp_dir) / "start-commit.session.part.avi"
            payload = self.recording_payload(
                save_path=str(final_path),
                recording_path=str(part_path),
                _write_part=True,
            )
            payload["recording_path"] = str(part_path)
            response_received = threading.Event()
            release_commit = threading.Event()
            original_request = self.supervisor._request_locked
            start_errors: list[BaseException] = []

            def pause_after_response(command: str, request_payload: Any, timeout_s: float) -> Any:
                result = original_request(command, request_payload, timeout_s)
                if command == "start_recording":
                    response_received.set()
                    if not release_commit.wait(2.0):
                        raise TimeoutError("test did not release start commit")
                return result

            def start() -> None:
                try:
                    self.supervisor.start_recording(payload)
                except BaseException as exc:
                    start_errors.append(exc)

            with mock.patch.object(self.supervisor, "_request_locked", side_effect=pause_after_response):
                start_thread = threading.Thread(target=start, daemon=True)
                start_thread.start()
                self.assertTrue(response_received.wait(2.0))
                shutdown_thread = threading.Thread(
                    target=lambda: self.supervisor.shutdown(timeout_s=2.0),
                    daemon=True,
                )
                shutdown_thread.start()
                deadline = time.monotonic() + 1.0
                while not self.supervisor._shutdown_requested and time.monotonic() < deadline:
                    time.sleep(0.005)
                release_commit.set()
                start_thread.join(2.0)
                shutdown_thread.join(3.0)

            self.assertFalse(start_thread.is_alive())
            self.assertFalse(shutdown_thread.is_alive())
            self.assertEqual(start_errors, [])
            self.assertEqual(final_path.read_bytes(), b"fake-avi")
            self.assertFalse(part_path.exists())
            status = self.supervisor.status()
            self.assertEqual(status["state"], "idle")
            self.assertEqual(status["last_video"]["saved_path"], str(final_path))

    def test_shutdown_waits_for_finalizing_stop_without_duplicate_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            final_path = Path(temp_dir) / "finalizing.avi"
            part_path = Path(temp_dir) / "finalizing.session.part.avi"
            payload = self.recording_payload(
                save_path=str(final_path),
                recording_path=str(part_path),
                _write_part=True,
            )
            payload["recording_path"] = str(part_path)
            self.supervisor.start_recording(payload)
            finalizing = threading.Event()
            release_finalization = threading.Event()
            original_finalize = self.supervisor._finalize_recording_video
            original_request = self.supervisor._request_locked
            commands: list[str] = []

            def count_request(command: str, request_payload: Any, timeout_s: float) -> Any:
                commands.append(command)
                return original_request(command, request_payload, timeout_s)

            def pause_finalization(result: Any, settings: Any) -> Any:
                finalizing.set()
                if not release_finalization.wait(2.0):
                    raise TimeoutError("test did not release video finalization")
                return original_finalize(result, settings)

            with (
                mock.patch.object(self.supervisor, "_request_locked", side_effect=count_request),
                mock.patch.object(
                    self.supervisor,
                    "_finalize_recording_video",
                    side_effect=pause_finalization,
                ),
            ):
                self.supervisor.begin_stop_recording()
                self.assertTrue(finalizing.wait(2.0))
                shutdown_thread = threading.Thread(
                    target=lambda: self.supervisor.shutdown(timeout_s=2.0),
                    daemon=True,
                )
                shutdown_thread.start()
                time.sleep(0.05)
                self.assertTrue(shutdown_thread.is_alive())
                release_finalization.set()
                shutdown_thread.join(3.0)

            self.assertFalse(shutdown_thread.is_alive())
            self.assertEqual(commands.count("stop_recording"), 1)
            self.assertEqual(final_path.read_bytes(), b"fake-avi")
            self.assertFalse(part_path.exists())
            status = self.supervisor.status()
            self.assertEqual(status["state"], "idle")
            self.assertEqual(status["last_video"]["saved_path"], str(final_path))

    def test_stale_start_context_cannot_resurrect_a_new_session(self) -> None:
        stale = self.supervisor._reserve_recording_start(self.recording_payload())
        with self.supervisor._state_lock:
            self.supervisor._clear_session(keep_error=True)
            self.assertTrue(
                self.supervisor._cancel_operation_locked(
                    stale,
                    supervisor_module._RecordingOperationPhase("reserved"),
                )
            )
        current = self.supervisor._reserve_recording_start(self.recording_payload())

        with self.assertRaises(CameraStateError):
            self.supervisor._complete_recording_start(stale)
        status = self.supervisor.status()
        self.assertEqual(status["state"], "starting")
        self.assertEqual(status["session_id"], current.session_id)

        self.supervisor._complete_recording_start(current)
        self.assertEqual(self.supervisor.status()["state"], "recording")
        self.supervisor.stop_recording()

    def test_reserved_start_uses_a_deep_immutable_payload_snapshot(self) -> None:
        payload = self.recording_payload(exposure_us=5000.0)
        context = self.supervisor._reserve_recording_start(payload)
        payload["settings"]["exposure_us"] = 20000.0
        payload["settings"]["nested"] = {"mutated": True}

        self.assertEqual(context.settings["exposure_us"], 5000.0)
        self.assertEqual(
            context.command_payload["settings"]["exposure_us"],
            5000.0,
        )
        self.assertNotIn("nested", context.command_payload["settings"])
        self.supervisor.shutdown(timeout_s=0.5)

    def test_same_part_and_final_path_must_still_exist_and_be_nonempty(self) -> None:
        missing_path = Path(self.temp_dir.name) / "same-path-missing.avi"
        payload = self.recording_payload(
            save_path=str(missing_path),
            recording_path=str(missing_path),
            _write_part=False,
        )
        self.supervisor.start_recording(payload)
        with self.assertRaises(CameraWorkerError) as caught:
            self.supervisor.stop_recording()
        self.assertEqual(caught.exception.error_type, "CAMERA_RECORD_FILE_MISSING")
        self.assertFalse(missing_path.exists())

    def test_runtime_exposure_and_gain_updates_prevent_stale_session_sharing(self) -> None:
        payload = self.recording_payload(
            serial_number="DA8583237",
            exposure_us=5000.0,
            gain=0.0,
        )
        self.supervisor.start_recording(payload)
        session_id = str(self.supervisor.status()["session_id"])
        self.supervisor.set_exposure(session_id, 20000.0)
        self.supervisor.set_gain(session_id, 1.5)
        settings = self.supervisor.status()["settings"]
        self.assertFalse(settings["exposure_auto"])
        self.assertEqual(settings["exposure_us"], 20000.0)
        self.assertEqual(settings["gain"], 1.5)

        with self.assertRaises(CameraStateError):
            self.supervisor.open_camera(
                {
                    "serial_number": "DA8583237",
                    "pixel_format": "mono8",
                    "exposure_us": 5000.0,
                    "gain": 0.0,
                }
            )
        shared = self.supervisor.open_camera(
            {
                "serial_number": "DA8583237",
                "pixel_format": "mono8",
                "exposure_auto": False,
                "exposure_us": 20000.0,
                "gain": 1.5,
            }
        )
        self.assertTrue(shared.recording_shared)
        self.supervisor.stop_recording()

    def test_shutdown_after_open_response_cannot_resurrect_session(self) -> None:
        response_received = threading.Event()
        release_commit = threading.Event()
        original_request = self.supervisor._request_locked
        open_errors: list[BaseException] = []
        shutdown_errors: list[BaseException] = []
        proxies: list[Any] = []

        def pause_after_open(command: str, request_payload: Any, timeout_s: float) -> Any:
            result = original_request(command, request_payload, timeout_s)
            if command == "open":
                response_received.set()
                if not release_commit.wait(2.0):
                    raise TimeoutError("test did not release open commit")
            return result

        def open_camera() -> None:
            try:
                proxies.append(
                    self.supervisor.open_camera(
                        {"device_index": 0, "pixel_format": "mono8"}
                    )
                )
            except BaseException as exc:
                open_errors.append(exc)

        def shut_down() -> None:
            try:
                self.supervisor.shutdown(timeout_s=2.0)
            except BaseException as exc:
                shutdown_errors.append(exc)

        with mock.patch.object(self.supervisor, "_request_locked", side_effect=pause_after_open):
            open_thread = threading.Thread(target=open_camera, daemon=True)
            open_thread.start()
            self.assertTrue(response_received.wait(2.0))
            shutdown_thread = threading.Thread(
                target=shut_down,
                daemon=True,
            )
            shutdown_thread.start()
            deadline = time.monotonic() + 1.0
            while not self.supervisor._shutdown_requested and time.monotonic() < deadline:
                time.sleep(0.005)
            release_commit.set()
            open_thread.join(2.0)
            shutdown_thread.join(3.0)

        self.assertFalse(open_thread.is_alive())
        self.assertFalse(shutdown_thread.is_alive())
        self.assertEqual(proxies, [])
        self.assertEqual(len(open_errors), 1)
        self.assertIsInstance(open_errors[0], CameraStateError)
        self.assertEqual(shutdown_errors, [])
        status = self.supervisor.status()
        self.assertEqual(status["state"], "idle")
        self.assertIsNone(status["worker_pid"])

    def test_capture_response_after_shutdown_cannot_restore_session_status(self) -> None:
        proxy = self.supervisor.open_camera({"device_index": 0, "pixel_format": "mono8"})
        response_received = threading.Event()
        release_commit = threading.Event()
        original_request = self.supervisor._request
        capture_results: list[Any] = []
        capture_errors: list[BaseException] = []

        def pause_after_capture(command: str, request_payload: Any, timeout_s: float) -> Any:
            result = original_request(command, request_payload, timeout_s)
            if command == "capture":
                response_received.set()
                if not release_commit.wait(3.0):
                    raise TimeoutError("test did not release capture commit")
            return result

        def capture() -> None:
            try:
                capture_results.append(proxy.capture_once("shutdown-race.bmp", timeout_ms=100))
            except BaseException as exc:
                capture_errors.append(exc)

        with mock.patch.object(self.supervisor, "_request", side_effect=pause_after_capture):
            capture_thread = threading.Thread(target=capture, daemon=True)
            capture_thread.start()
            self.assertTrue(response_received.wait(2.0))

            self.supervisor.shutdown(timeout_s=2.0)
            self.assertEqual(self.supervisor.status()["state"], "idle")
            self.assertEqual(self.supervisor._latest_status, {})

            release_commit.set()
            capture_thread.join(2.0)

        self.assertFalse(capture_thread.is_alive())
        self.assertEqual(capture_errors, [])
        self.assertEqual(len(capture_results), 1)
        self.assertEqual(capture_results[0].frame_num, 1)
        self.assertEqual(self.supervisor.status()["state"], "idle")
        self.assertEqual(self.supervisor._latest_status, {})

    def test_shutdown_does_not_join_a_reserved_thread_before_it_starts(self) -> None:
        context = self.supervisor._reserve_recording_start(self.recording_payload())
        not_started = threading.Thread(target=lambda: None, daemon=True)
        with self.supervisor._state_lock:
            self.supervisor._operation_thread = not_started

        self.supervisor.shutdown(timeout_s=0.2)

        self.assertTrue(context.done_event.is_set())
        self.assertEqual(self.supervisor.status()["state"], "idle")

    def test_shutdown_completes_a_reserved_stop_with_an_unstarted_owner_thread(self) -> None:
        self.supervisor.start_recording(self.recording_payload())
        context = self.supervisor._reserve_recording_stop()
        not_started = threading.Thread(target=lambda: None, daemon=True)
        with self.supervisor._state_lock:
            self.supervisor._operation_thread = not_started

        self.supervisor.shutdown(timeout_s=2.0)

        self.assertTrue(context.done_event.is_set())
        status = self.supervisor.status()
        self.assertEqual(status["state"], "idle")
        self.assertTrue(Path(status["last_video"]["saved_path"]).is_file())

    def test_shutdown_keeps_finalizing_token_until_delayed_commit_finishes(self) -> None:
        finalizing = threading.Event()
        release_finalization = threading.Event()
        original_finalize = self.supervisor._finalize_recording_video

        def pause_finalization(result: Any, settings: Any) -> Any:
            finalizing.set()
            release_finalization.wait(5.0)
            return original_finalize(result, settings)

        with mock.patch.object(
            self.supervisor,
            "_finalize_recording_video",
            side_effect=pause_finalization,
        ):
            self.supervisor.start_recording(self.recording_payload())
            self.supervisor.begin_stop_recording()
            self.assertTrue(finalizing.wait(2.0))
            started = time.monotonic()
            self.supervisor.shutdown(timeout_s=0.2)
            self.assertLess(time.monotonic() - started, 3.5)
            pending = self.supervisor.status()
            self.assertEqual(pending["state"], "stopping")
            self.assertEqual(
                pending["error_code"],
                "CAMERA_SHUTDOWN_FINALIZING",
            )
            release_finalization.set()
            deadline = time.monotonic() + 2.0
            while self.supervisor.status()["state"] != "idle" and time.monotonic() < deadline:
                time.sleep(0.01)

        completed = self.supervisor.status()
        self.assertEqual(completed["state"], "idle")
        self.assertIsNone(completed["error"])
        self.assertTrue(Path(completed["last_video"]["saved_path"]).is_file())

    def test_async_start_thread_creation_failure_does_not_poison_state(self) -> None:
        original_thread = supervisor_module.threading.Thread

        class FailingThread:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("injected thread start failure")

        supervisor_module.threading.Thread = FailingThread  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                self.supervisor.begin_start_recording(self.recording_payload())
        finally:
            supervisor_module.threading.Thread = original_thread
        status = self.supervisor.status()
        self.assertEqual(status["state"], "idle")
        self.assertFalse(self.supervisor.is_recording_busy())

    def test_async_stop_thread_creation_failure_rolls_back_to_recording(self) -> None:
        self.supervisor.start_recording(self.recording_payload())
        original_thread = supervisor_module.threading.Thread

        class FailingThread:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("injected thread start failure")

        supervisor_module.threading.Thread = FailingThread  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                self.supervisor.begin_stop_recording()
        finally:
            supervisor_module.threading.Thread = original_thread
        status = self.supervisor.status()
        self.assertEqual(status["state"], "recording")
        self.assertTrue(self.supervisor.is_recording_busy())
        self.assertIn("thread start failure", str(status["error"]))
        self.supervisor.stop_recording()


if __name__ == "__main__":
    unittest.main(verbosity=2)
