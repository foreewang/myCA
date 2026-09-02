"""Service-layer ownership contracts for asynchronous camera recording."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class RequestStub:
    save_path = "data/camera_records/service_contract.avi"
    camera_path = None
    device_index = None
    serial_number = None
    ip = None
    mvs_python_dir = None
    pixel_format = None
    exposure_us = None
    gain = None
    fps = 10.0
    bitrate_kbps = 1000
    timeout_ms = 2_005


import workflow.camera_executor as camera_executor  # noqa: E402
import camera_controller  # noqa: E402
import workflow.camera_record_service as camera_record_service  # noqa: E402
import workflow.hardware_guard as hardware_guard  # noqa: E402


class CameraRecordServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        hardware_guard.reset_hardware_owners()
        self.busy = False
        self.camera_state = "idle"
        self.start_error: BaseException | None = None
        self.stop_error: BaseException | None = None
        self.start_kwargs: dict[str, Any] = {}
        self.stop_result: dict[str, Any] = {"status": "stopping"}

        def start_recording_camera(**kwargs: Any) -> dict[str, Any]:
            self.start_kwargs = dict(kwargs)
            if self.start_error is not None:
                raise self.start_error
            self.busy = True
            self.camera_state = "starting"
            return {"state": "starting", "operation_id": "op-start"}

        def stop_recording_camera() -> dict[str, Any]:
            if self.stop_error is not None:
                raise self.stop_error
            return dict(self.stop_result)

        def resolve_timeout(timeout_ms: int | None, *, exposure_us: float | None = None) -> int:
            exposure_ms = int(((exposure_us or 0) + 999) / 1000)
            minimum = exposure_ms + 2_000
            value = max(1_500, minimum) if timeout_ms is None else int(timeout_ms)
            if value < minimum or value > 15_000:
                raise camera_controller.CameraSDKError("invalid timeout")
            return value

        self.patchers = (
            mock.patch.object(camera_executor, "start_recording_camera", side_effect=start_recording_camera),
            mock.patch.object(camera_executor, "stop_recording_camera", side_effect=stop_recording_camera),
            mock.patch.object(camera_executor, "recording_camera_is_busy", side_effect=lambda: self.busy),
            mock.patch.object(
                camera_executor,
                "recording_camera_status",
                side_effect=lambda: {"state": self.camera_state if self.busy else "idle"},
            ),
            mock.patch.object(
                camera_controller,
                "resolve_record_grab_timeout_ms",
                side_effect=resolve_timeout,
            ),
        )
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        hardware_guard.reset_hardware_owners()
        for patcher in reversed(self.patchers):
            patcher.stop()

    @staticmethod
    def settings(_request: RequestStub) -> dict[str, Any]:
        return {
            "mvs_python_dir": "MVS",
            "device_index": 0,
            "serial_number": "SERIAL-1",
            "camera_ip": "192.168.1.53",
            "pixel_format": "mono8",
            "exposure_us": 5_000,
            "gain": 0.0,
            "camera_path": "config/camera.yaml",
        }

    def test_accepted_start_keeps_owner_and_passes_effective_settings(self) -> None:
        result = camera_record_service.start_camera_recording(
            RequestStub(),
            settings_loader=self.settings,
        )
        self.assertEqual(result["state"], "starting")
        self.assertEqual(result["camera_path"], "config/camera.yaml")
        self.assertEqual(self.start_kwargs["timeout_ms"], 2_005)
        self.assertEqual(self.start_kwargs["camera_path"], "config/camera.yaml")
        owners = hardware_guard.current_hardware_owners()
        self.assertEqual([owner["kind"] for owner in owners], ["camera_record"])

    def test_scheduling_failure_releases_owner(self) -> None:
        self.start_error = RuntimeError("injected scheduling failure")
        with self.assertRaises(camera_record_service.CameraRecordServiceError):
            camera_record_service.start_camera_recording(
                RequestStub(),
                settings_loader=self.settings,
            )
        self.assertEqual(hardware_guard.current_hardware_owners(), [])

    def test_failure_after_202_releases_owner_on_next_sync(self) -> None:
        camera_record_service.start_camera_recording(
            RequestStub(),
            settings_loader=self.settings,
        )
        self.busy = False
        self.assertEqual(hardware_guard.current_hardware_owners(), [])

    def test_async_stop_keeps_owner_until_supervisor_reaches_terminal_state(self) -> None:
        hardware_guard.acquire_hardware_operation(
            "camera_record", str(PROJECT_ROOT / "data" / "camera_records" / "active.avi")
        )
        self.busy = True
        self.camera_state = "recording"
        result = camera_record_service.stop_camera_recording()
        self.assertEqual(result["status"], "stopping")
        self.assertEqual(
            [owner["kind"] for owner in hardware_guard.current_hardware_owners()],
            ["camera_record"],
        )

    def test_immediate_stop_failure_releases_stale_owner(self) -> None:
        hardware_guard.acquire_hardware_operation(
            "camera_record", str(PROJECT_ROOT / "data" / "camera_records" / "active.avi")
        )
        self.busy = False
        self.stop_error = RuntimeError("no active recording")
        with self.assertRaises(camera_record_service.CameraRecordServiceError):
            camera_record_service.stop_camera_recording()
        self.assertEqual(hardware_guard.current_hardware_owners(), [])

    def test_stop_scheduling_failure_rolls_back_transition_marker(self) -> None:
        hardware_guard.acquire_hardware_operation("camera_record", "active.avi")
        self.busy = True
        self.camera_state = "recording"
        self.stop_error = RuntimeError("injected stop scheduling failure")

        with self.assertRaises(camera_record_service.CameraRecordServiceError):
            camera_record_service.stop_camera_recording()

        owner = hardware_guard.acquire_hardware_operation("task", "task-after-failed-stop")
        self.assertEqual(owner["operation_id"], "task-after-failed-stop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
