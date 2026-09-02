"""Hardware-guard tests for asynchronous camera recording transitions."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import workflow.hardware_guard as hardware_guard  # noqa: E402


class HardwareGuardCameraTests(unittest.TestCase):
    def setUp(self) -> None:
        hardware_guard.reset_hardware_owners()
        self.state = "idle"
        self.busy = False
        self.original_executor = sys.modules.get("workflow.camera_executor")
        fake_executor = types.ModuleType("workflow.camera_executor")
        fake_executor.recording_camera_status = lambda: {"state": self.state}  # type: ignore[attr-defined]
        fake_executor.recording_camera_is_busy = lambda: self.busy  # type: ignore[attr-defined]
        sys.modules["workflow.camera_executor"] = fake_executor

    def tearDown(self) -> None:
        hardware_guard.reset_hardware_owners()
        if self.original_executor is None:
            sys.modules.pop("workflow.camera_executor", None)
        else:
            sys.modules["workflow.camera_executor"] = self.original_executor

    def test_other_operation_is_rejected_while_recording_is_starting(self) -> None:
        hardware_guard.acquire_hardware_operation("camera_record", "recording.avi")
        self.state = "starting"
        self.busy = True

        with self.assertRaises(hardware_guard.HardwareGuardError) as caught:
            hardware_guard.acquire_hardware_operation("task", "task-1")
        self.assertEqual(caught.exception.error_code, "CAMERA_RECORD_TRANSITION")

    def test_compatible_operation_is_allowed_after_recording_is_stable(self) -> None:
        hardware_guard.acquire_hardware_operation("camera_record", "recording.avi")
        self.state = "recording"
        self.busy = True

        owner = hardware_guard.acquire_hardware_operation("task", "task-1")
        self.assertEqual(owner["kind"], "task")
        kinds = {item["kind"] for item in hardware_guard.current_hardware_owners()}
        self.assertEqual(kinds, {"camera_record", "task"})

    def test_confirmed_async_start_releases_stale_owner_on_next_sync(self) -> None:
        hardware_guard.acquire_hardware_operation("camera_record", "recording.avi")
        hardware_guard.confirm_hardware_operation("camera_record", "recording.avi")
        self.state = "idle"
        self.busy = False

        owner = hardware_guard.acquire_hardware_operation("task", "task-1")
        self.assertEqual(owner["kind"], "task")
        self.assertNotIn(
            "camera_record",
            {item["kind"] for item in hardware_guard.current_hardware_owners()},
        )

    def test_stop_reservation_blocks_a_new_shared_hardware_operation(self) -> None:
        hardware_guard.acquire_hardware_operation("camera_record", "recording.avi")
        self.state = "recording"
        self.busy = True

        reservation = hardware_guard.begin_camera_record_stop_transition()
        self.assertEqual(reservation["operation_id"], "recording.avi")
        with self.assertRaises(hardware_guard.HardwareGuardError) as caught:
            hardware_guard.acquire_hardware_operation("task", "task-after-stop")
        self.assertEqual(caught.exception.error_code, "CAMERA_RECORD_TRANSITION")

    def test_new_camera_owner_between_snapshot_and_lock_is_not_missed(self) -> None:
        original_snapshot = hardware_guard._camera_record_transition_snapshot

        def inject_camera_start_race():
            with hardware_guard._HARDWARE_OPERATION_LOCK:
                hardware_guard._CAMERA_RECORD_OWNER = {
                    "kind": "camera_record",
                    "operation_id": "racing-recording.avi",
                    "started_at": "now",
                    "sync_after_monotonic": 0.0,
                }
            self.state = "starting"
            self.busy = True
            return None, None

        hardware_guard._camera_record_transition_snapshot = inject_camera_start_race
        try:
            with self.assertRaises(hardware_guard.HardwareGuardError) as caught:
                hardware_guard.acquire_hardware_operation("task", "task-racing-start")
        finally:
            hardware_guard._camera_record_transition_snapshot = original_snapshot
        self.assertEqual(caught.exception.error_code, "CAMERA_RECORD_TRANSITION")
        self.assertNotIn(
            "task",
            {owner["kind"] for owner in hardware_guard.current_hardware_owners()},
        )

    def test_active_shared_operation_blocks_stop_and_cancel_restores_admission(self) -> None:
        hardware_guard.acquire_hardware_operation("camera_record", "recording.avi")
        self.state = "recording"
        self.busy = True
        hardware_guard.acquire_hardware_operation("task", "task-1")

        with self.assertRaises(hardware_guard.HardwareGuardError) as caught:
            hardware_guard.begin_camera_record_stop_transition()
        self.assertEqual(caught.exception.error_code, "CAMERA_RECORD_STOP_BLOCKED")

        hardware_guard.release_hardware_operation("task", "task-1")
        reservation = hardware_guard.begin_camera_record_stop_transition()
        hardware_guard.cancel_camera_record_stop_transition(reservation["operation_id"])
        owner = hardware_guard.acquire_hardware_operation("task", "task-2")
        self.assertEqual(owner["operation_id"], "task-2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
