"""Hardware-guard tests for asynchronous camera recording transitions."""
from __future__ import annotations

import multiprocessing
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import workflow.hardware_guard as hardware_guard  # noqa: E402


def _crash_after_generation_marker(path: str, ready) -> None:
    """Spawn-safe helper that bypasses every Python cleanup handler."""

    import workflow.hardware_guard as child_guard

    child_guard.restore_persisted_hardware_safety_latch(Path(path))
    ready.set()
    os._exit(23)


class HardwareGuardCameraTests(unittest.TestCase):
    def setUp(self) -> None:
        hardware_guard._reset_hardware_guard_for_tests()
        self.state = "idle"
        self.busy = False
        self.original_executor = sys.modules.get("workflow.camera_executor")
        fake_executor = types.ModuleType("workflow.camera_executor")
        fake_executor.recording_camera_status = lambda: {"state": self.state}  # type: ignore[attr-defined]
        fake_executor.recording_camera_is_busy = lambda: self.busy  # type: ignore[attr-defined]
        sys.modules["workflow.camera_executor"] = fake_executor

    def tearDown(self) -> None:
        hardware_guard._reset_hardware_guard_for_tests()
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

    def test_pending_camera_owner_cannot_be_reaped_before_start_confirmation(self) -> None:
        hardware_guard.acquire_hardware_operation("camera_record", "recording.avi")
        with hardware_guard._HARDWARE_OPERATION_LOCK:
            hardware_guard._CAMERA_RECORD_OWNER["sync_after_monotonic"] = 0.0
        self.state = "idle"
        self.busy = False

        with self.assertRaises(hardware_guard.HardwareGuardError) as caught:
            hardware_guard.acquire_hardware_operation("task", "must-not-pass")
        self.assertEqual(caught.exception.error_code, "CAMERA_RECORD_TRANSITION")

        hardware_guard.confirm_hardware_operation("camera_record", "recording.avi")
        owner = hardware_guard.acquire_hardware_operation("task", "allowed-after-terminal")
        self.assertEqual(owner["operation_id"], "allowed-after-terminal")

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

    def test_safety_latch_broadcasts_cancel_and_stage_stop_for_existing_owner(self) -> None:
        hardware_guard.acquire_hardware_operation("task", "task-before-latch")
        with mock.patch(
            "workflow.task_runtime.request_all_task_cancellations"
        ) as cancel_all, mock.patch(
            "workflow.stage_reciprocation.stage_reciprocation_controller.request_stop"
        ) as request_stage_stop:
            hardware_guard.latch_hardware_unsafe(
                "INJECTED_UNSAFE",
                "injected unsafe hardware state",
            )

        cancel_all.assert_called_once_with()
        request_stage_stop.assert_called_once_with()
        with self.assertRaises(hardware_guard.HardwareGuardError) as caught:
            hardware_guard.require_hardware_safe()
        self.assertEqual(caught.exception.error_code, "HARDWARE_SAFETY_LATCHED")

    def test_persisted_safety_latch_survives_process_state_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            latch_path = Path(temp_dir) / "hardware_safety_latch.json"
            hardware_guard._reset_hardware_guard_for_tests(forget_persistence_path=True)
            hardware_guard.restore_persisted_hardware_safety_latch(latch_path)
            with mock.patch(
                "workflow.task_runtime.request_all_task_cancellations"
            ), mock.patch(
                "workflow.stage_reciprocation.stage_reciprocation_controller.request_stop"
            ):
                latched = hardware_guard.latch_hardware_unsafe(
                    "INJECTED_PERSISTENT_UNSAFE",
                    "injected persistent unsafe hardware state",
                )

            self.assertFalse(latched["safe"])
            self.assertTrue(latched["persistence_confirmed"])
            self.assertTrue(latch_path.is_file())

            # Simulate a fresh process generation without deleting disk state.
            hardware_guard._reset_hardware_guard_for_tests()
            self.assertTrue(hardware_guard.hardware_safety_status()["safe"])
            restored = hardware_guard.restore_persisted_hardware_safety_latch(latch_path)
            self.assertFalse(restored["safe"])
            self.assertEqual(restored["error_code"], "INJECTED_PERSISTENT_UNSAFE")
            self.assertTrue(restored["persistence_confirmed"])
            with self.assertRaises(hardware_guard.HardwareGuardError):
                hardware_guard.acquire_hardware_operation("task", "blocked-after-restart")

            hardware_guard._reset_hardware_guard_for_tests(
                clear_persisted_latch=True,
                forget_persistence_path=True,
            )

    def test_invalid_persisted_latch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            latch_path = Path(temp_dir) / "hardware_safety_latch.json"
            latch_path.write_text('{"safe": true}', encoding="utf-8")
            hardware_guard._reset_hardware_guard_for_tests(forget_persistence_path=True)

            restored = hardware_guard.restore_persisted_hardware_safety_latch(latch_path)

            self.assertFalse(restored["safe"])
            self.assertEqual(
                restored["error_code"],
                "HARDWARE_SAFETY_LATCH_FILE_INVALID",
            )
            self.assertTrue(restored["persistence_confirmed"])
            with self.assertRaises(hardware_guard.HardwareGuardError):
                hardware_guard.require_hardware_safe()

            hardware_guard._reset_hardware_guard_for_tests(
                clear_persisted_latch=True,
                forget_persistence_path=True,
            )

    def test_persistence_failure_keeps_generation_fence_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            latch_path = Path(temp_dir) / "hardware_safety_latch.json"
            hardware_guard._reset_hardware_guard_for_tests(forget_persistence_path=True)
            hardware_guard.restore_persisted_hardware_safety_latch(latch_path)
            with mock.patch(
                "workflow.task_runtime.request_all_task_cancellations"
            ), mock.patch(
                "workflow.stage_reciprocation.stage_reciprocation_controller.request_stop"
            ), mock.patch.object(
                hardware_guard,
                "atomic_write_json",
                side_effect=OSError("injected persistence failure"),
            ):
                latched = hardware_guard.latch_hardware_unsafe(
                    "INJECTED_UNPERSISTED_UNSAFE",
                    "injected unsafe hardware state",
                )
                durable = hardware_guard.hardware_safety_fence_is_durable()

            self.assertFalse(latched["persistence_confirmed"])
            self.assertFalse(durable)
            persisted_during_crash_window = hardware_guard.read_json_with_retry(latch_path)
            self.assertEqual(
                persisted_during_crash_window["record_type"],
                "generation_active",
            )

            # Even if the process dies before the unsafe overwrite succeeds,
            # the write-ahead marker makes the next generation fail closed.
            hardware_guard._reset_hardware_guard_for_tests()
            restored = hardware_guard.restore_persisted_hardware_safety_latch(latch_path)
            self.assertFalse(restored["safe"])
            self.assertEqual(
                restored["error_code"],
                "HARDWARE_GENERATION_UNCLEAN_EXIT",
            )
            self.assertTrue(restored["persistence_confirmed"])
            hardware_guard._reset_hardware_guard_for_tests(
                clear_persisted_latch=True,
                forget_persistence_path=True,
            )

    def test_real_abrupt_process_exit_is_latched_by_next_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            latch_path = Path(temp_dir) / "hardware_safety_latch.json"
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            process = context.Process(
                target=_crash_after_generation_marker,
                args=(str(latch_path), ready),
                name="hardware-generation-crash-test",
            )
            process.start()
            try:
                self.assertTrue(ready.wait(timeout=5.0))
                process.join(timeout=5.0)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 23)
                marker = hardware_guard.read_json_with_retry(latch_path)
                self.assertEqual(marker["record_type"], "generation_active")

                hardware_guard._reset_hardware_guard_for_tests(
                    forget_persistence_path=True
                )
                restored = hardware_guard.restore_persisted_hardware_safety_latch(
                    latch_path
                )
                self.assertFalse(restored["safe"])
                self.assertEqual(
                    restored["error_code"],
                    "HARDWARE_GENERATION_UNCLEAN_EXIT",
                )
                self.assertTrue(restored["persistence_confirmed"])
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2.0)
                hardware_guard._reset_hardware_guard_for_tests(
                    clear_persisted_latch=True,
                    forget_persistence_path=True,
                )

    def test_clean_shutdown_is_the_only_path_that_removes_generation_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            latch_path = Path(temp_dir) / "hardware_safety_latch.json"
            hardware_guard._reset_hardware_guard_for_tests(forget_persistence_path=True)
            started = hardware_guard.restore_persisted_hardware_safety_latch(latch_path)
            self.assertTrue(started["safe"])
            self.assertTrue(latch_path.is_file())

            # An open admission gate means the process can still start work;
            # the write-ahead marker must not be removed yet.
            self.assertFalse(hardware_guard.hardware_safety_shutdown_fence_ready())
            self.assertTrue(latch_path.is_file())

            hardware_guard.close_hardware_admission_for_shutdown()
            self.assertTrue(hardware_guard.hardware_safety_shutdown_fence_ready())
            self.assertFalse(latch_path.exists())
            hardware_guard._reset_hardware_guard_for_tests(forget_persistence_path=True)

    def test_restore_recreates_durable_latch_if_existing_marker_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            latch_path = Path(temp_dir) / "hardware_safety_latch.json"
            hardware_guard._reset_hardware_guard_for_tests(forget_persistence_path=True)
            hardware_guard.restore_persisted_hardware_safety_latch(latch_path)
            hardware_guard._reset_hardware_guard_for_tests()
            original_reader = hardware_guard.read_json_with_retry
            calls = 0

            def disappearing_reader(path, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    latch_path.unlink()
                    raise FileNotFoundError("injected marker disappearance")
                return original_reader(path, *args, **kwargs)

            with mock.patch.object(
                hardware_guard,
                "read_json_with_retry",
                side_effect=disappearing_reader,
            ):
                restored = hardware_guard.restore_persisted_hardware_safety_latch(
                    latch_path
                )

            self.assertFalse(restored["safe"])
            self.assertEqual(
                restored["error_code"],
                "HARDWARE_SAFETY_LATCH_FILE_INVALID",
            )
            self.assertTrue(restored["persistence_confirmed"])
            self.assertEqual(
                original_reader(latch_path)["record_type"],
                "unsafe_latch",
            )
            hardware_guard._reset_hardware_guard_for_tests(
                clear_persisted_latch=True,
                forget_persistence_path=True,
            )

    def test_pending_stage_owner_cannot_be_reaped_before_start_confirmation(self) -> None:
        import workflow.stage_reciprocation as stage_module

        hardware_guard.acquire_hardware_operation(
            "stage_reciprocation",
            "stage_reciprocation",
        )
        with hardware_guard._HARDWARE_OPERATION_LOCK:
            hardware_guard._HARDWARE_OWNER["sync_after_monotonic"] = 0.0

        with mock.patch.object(
            stage_module.stage_reciprocation_controller,
            "status",
            return_value={"status": "stopped"},
        ):
            self.assertIsNotNone(hardware_guard.current_hardware_owner())
            hardware_guard.confirm_hardware_operation(
                "stage_reciprocation",
                "stage_reciprocation",
            )
            self.assertIsNone(hardware_guard.current_hardware_owner())

    def test_stage_status_sync_never_holds_hardware_admission_lock(self) -> None:
        import workflow.stage_reciprocation as stage_module

        with hardware_guard._HARDWARE_OPERATION_LOCK:
            hardware_guard._HARDWARE_OWNER = {
                "kind": "stage_reciprocation",
                "operation_id": "stage_reciprocation",
                "started_at": "now",
                "sync_after_monotonic": 0.0,
                "sync_enabled": True,
            }

        status_entered = threading.Event()
        release_status = threading.Event()

        def blocking_status():
            status_entered.set()
            if not release_status.wait(timeout=2.0):
                raise RuntimeError("test did not release stage status")
            return {"status": "running"}

        with mock.patch.object(
            stage_module.stage_reciprocation_controller,
            "status",
            side_effect=blocking_status,
        ):
            reader = threading.Thread(target=hardware_guard.current_hardware_owner)
            reader.start()
            self.assertTrue(status_entered.wait(timeout=1.0))
            acquired = hardware_guard._HARDWARE_OPERATION_LOCK.acquire(timeout=0.5)
            try:
                self.assertTrue(
                    acquired,
                    "stage status was called while the hardware lock was held",
                )
            finally:
                if acquired:
                    hardware_guard._HARDWARE_OPERATION_LOCK.release()
                release_status.set()
            reader.join(timeout=1.0)
            self.assertFalse(reader.is_alive(), "owner sync thread deadlocked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
