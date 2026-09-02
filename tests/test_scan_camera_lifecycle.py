"""Regression test for repeated autofocus/capture camera lifecycles."""
from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


_saved_camera_executor = sys.modules.get("workflow.camera_executor")
_saved_stage_executor = sys.modules.get("workflow.stage_executor")
camera_stub = types.ModuleType("workflow.camera_executor")
camera_stub.open_camera = lambda **kwargs: None  # type: ignore[attr-defined]
camera_stub.close_camera = lambda camera: None  # type: ignore[attr-defined]
camera_stub.capture_with_opened_camera = lambda **kwargs: {}  # type: ignore[attr-defined]
stage_stub = types.ModuleType("workflow.stage_executor")
stage_stub.move_to_absolute = lambda **kwargs: {}  # type: ignore[attr-defined]
sys.modules["workflow.camera_executor"] = camera_stub
sys.modules["workflow.stage_executor"] = stage_stub
try:
    scan_executor = importlib.import_module("workflow.scan_executor")
finally:
    if _saved_camera_executor is None:
        sys.modules.pop("workflow.camera_executor", None)
    else:
        sys.modules["workflow.camera_executor"] = _saved_camera_executor
    if _saved_stage_executor is None:
        sys.modules.pop("workflow.stage_executor", None)
    else:
        sys.modules["workflow.stage_executor"] = _saved_stage_executor


class ScanCameraLifecycleTests(unittest.TestCase):
    def test_owned_capture_session_is_closed_before_each_new_autofocus(self) -> None:
        events: list[str] = []
        opened = 0

        class Camera:
            def __init__(self, index: int) -> None:
                self.index = index

        def open_camera(**kwargs: Any) -> Camera:
            nonlocal opened
            opened += 1
            camera = Camera(opened)
            events.append(f"open:{camera.index}")
            return camera

        def close_camera(camera: Camera) -> None:
            events.append(f"close:{camera.index}")

        def autofocus(**kwargs: Any) -> dict[str, Any]:
            events.append("autofocus")
            return {"status": "success"}

        def capture(**kwargs: Any) -> dict[str, Any]:
            camera = kwargs["cam"]
            events.append(f"capture:{camera.index}")
            return {"saved_path": f"image-{camera.index}.bmp"}

        originals = {
            "open_camera": scan_executor.open_camera,
            "close_camera": scan_executor.close_camera,
            "capture_with_opened_camera": scan_executor.capture_with_opened_camera,
            "move_to_absolute": scan_executor.move_to_absolute,
            "_check_motion_guard": scan_executor._check_motion_guard,
            "_should_run_autofocus_at_this_point": scan_executor._should_run_autofocus_at_this_point,
            "_execute_autofocus_before_capture": scan_executor._execute_autofocus_before_capture,
            "_write_result": scan_executor._write_result,
            "report_progress": scan_executor.report_progress,
            "raise_if_cancel_requested": scan_executor.raise_if_cancel_requested,
        }
        scan_executor.open_camera = open_camera
        scan_executor.close_camera = close_camera
        scan_executor.capture_with_opened_camera = capture
        scan_executor.move_to_absolute = lambda **kwargs: {"status": "success"}
        scan_executor._check_motion_guard = lambda *args, **kwargs: None
        scan_executor._should_run_autofocus_at_this_point = lambda params, point: (True, "per_point")
        scan_executor._execute_autofocus_before_capture = autofocus
        scan_executor._write_result = lambda *args, **kwargs: None
        scan_executor.report_progress = lambda *args, **kwargs: None
        scan_executor.raise_if_cancel_requested = lambda *args, **kwargs: None
        try:
            points = [
                {
                    "index": index,
                    "row_index": 0,
                    "col_index": index - 1,
                    "view_down_mm": 0.0,
                    "view_right_mm": 0.0,
                    "stage_x_target": index,
                    "stage_y_target": index,
                }
                for index in (1, 2)
            ]
            result = scan_executor.execute_scan_capture(
                {"plate": {"stage_limits": {}}, "task": {}},
                {
                    "task_id": "scan-camera-lifecycle",
                    "task_type": "capture",
                    "plate_type": "24-well",
                    "well_name": "A1",
                    "objective_name": "4x",
                    "motion": {
                        "profile_vel": 1,
                        "profile_acc": 1,
                        "profile_dec": 1,
                    },
                    "settle_s": 0.0,
                    "save_dir": "data/test",
                    "filename_pattern": "{index}.bmp",
                    "device_index": 0,
                    "pixel_format": "mono8",
                    "autofocus_decision": {"should_run": True},
                },
                {
                    "points": points,
                    "reference": {},
                    "scan_config": {},
                    "stage_limit_precheck": {},
                },
            )
        finally:
            for name, value in originals.items():
                setattr(scan_executor, name, value)

        self.assertEqual(result["status"], "success")
        self.assertEqual(
            events,
            [
                "autofocus",
                "open:1",
                "capture:1",
                "close:1",
                "autofocus",
                "open:2",
                "capture:2",
                "close:2",
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
