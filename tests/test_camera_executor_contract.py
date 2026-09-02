"""Workflow-layer contracts for the isolated camera supervisor."""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEVICES_DIR = PROJECT_ROOT / "devices"
for _path in (str(PROJECT_ROOT), str(DEVICES_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


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


import workflow.camera_executor as camera_executor  # noqa: E402


class FakeSupervisor:
    def __init__(self) -> None:
        self.begin_payload: dict[str, Any] | None = None
        self.begin_stop_calls = 0
        self.current_status: dict[str, Any] = {
            "state": "idle",
            "recording": False,
            "background": False,
            "settings": {},
        }

    def begin_start_recording(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.begin_payload = payload
        self.current_status = {
            "state": "starting",
            "recording": False,
            "background": False,
            "starting": True,
            "stopping": False,
            "operation_id": "op-start",
            "settings": dict(payload["settings"]),
        }
        return dict(self.current_status)

    def begin_stop_recording(self) -> dict[str, Any]:
        self.begin_stop_calls += 1
        self.current_status = {
            **self.current_status,
            "state": "stopping",
            "recording": False,
            "background": False,
            "starting": False,
            "stopping": True,
        }
        return dict(self.current_status)

    def status(self) -> dict[str, Any]:
        return dict(self.current_status)

    def is_recording_busy(self) -> bool:
        return self.current_status.get("state") in {"starting", "recording", "stopping"}


class CameraExecutorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeSupervisor()
        self.original_getter = camera_executor.get_camera_process_supervisor
        camera_executor.get_camera_process_supervisor = lambda: self.fake  # type: ignore[assignment]

    def tearDown(self) -> None:
        camera_executor.get_camera_process_supervisor = self.original_getter

    def test_start_uses_async_supervisor_and_hides_part_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            final_path = Path(temp_dir) / "recording.avi"
            result = camera_executor.start_recording_camera(
                save_path=str(final_path),
                camera_path="config/camera.yaml",
                serial_number="SERIAL-1",
                exposure_us=5_000,
                timeout_ms=2_005,
            )

        self.assertEqual(result["state"], "starting")
        self.assertEqual(result["operation_id"], "op-start")
        self.assertEqual(result["saved_path"], str(final_path))
        self.assertNotIn("recording_path", result["settings"])
        self.assertIsNotNone(self.fake.begin_payload)
        assert self.fake.begin_payload is not None
        self.assertTrue(str(self.fake.begin_payload["recording_path"]).endswith(".part.avi"))
        self.assertEqual(self.fake.begin_payload["settings"]["camera_path"], "config/camera.yaml")
        self.assertEqual(self.fake.begin_payload["timeout_ms"], 2_005)

    def test_stop_returns_transition_state_without_waiting_for_video(self) -> None:
        self.fake.current_status = {
            "state": "recording",
            "recording": True,
            "background": True,
            "operation_id": "op-start",
            "worker_pid": 321,
            "settings": {
                "save_path": "final.avi",
                "recording_path": "final.part.avi",
            },
        }
        result = camera_executor.stop_recording_camera()
        self.assertEqual(result["status"], "stopping")
        self.assertEqual(result["state"], "stopping")
        self.assertEqual(result["worker_pid"], 321)
        self.assertEqual(result["video"], {})
        self.assertEqual(result["saved_path"], "final.avi")
        self.assertNotIn("recording_path", result["settings"])
        self.assertEqual(self.fake.begin_stop_calls, 1)

    def test_status_is_local_and_reports_public_final_path(self) -> None:
        self.fake.current_status = {
            "state": "recording",
            "recording": True,
            "background": True,
            "saved_path": "internal.part.avi",
            "settings": {
                "save_path": "public.avi",
                "recording_path": "internal.part.avi",
            },
        }
        status = camera_executor.recording_camera_status()
        self.assertEqual(status["saved_path"], "public.avi")
        self.assertNotIn("recording_path", status["settings"])
        self.assertTrue(camera_executor.recording_camera_is_busy())


if __name__ == "__main__":
    unittest.main(verbosity=2)
