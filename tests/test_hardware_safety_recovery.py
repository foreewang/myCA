"""Safety and audit tests for the offline latch-clearance protocol."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from tools import clear_hardware_safety_latch as recovery
from workflow import hardware_guard
from workflow.process_guard import SingleInstanceLock, SingleWorkerGuardError


def _marker_bytes() -> bytes:
    return json.dumps(
        {
            "version": 1,
            "record_type": "unsafe_latch",
            "safe": False,
            "latch_id": "test-latch-id",
            "error_code": "INJECTED_UNSAFE",
            "message": "injected unsafe state",
            "latched_at": "2026-09-03T00:00:00+00:00",
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")


def _paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "latch_path": tmp_path / "hardware_safety_latch.json",
        "api_lock_path": tmp_path / "api_server.lock",
        "camera_worker_lock_path": tmp_path / "camera_worker.lock",
        "archive_dir": tmp_path / "clearance_archive",
    }


def _clear(tmp_path: Path, **overrides):
    values = {
        "operator": "operator-001",
        "reason": "physical stop and driver inspection completed",
        "confirmation": recovery.CONFIRMATION_PHRASE,
        **_paths(tmp_path),
    }
    values.update(overrides)
    return recovery._clear_hardware_safety_latch_with_paths(**values)


def test_exact_confirmation_operator_and_reason_are_mandatory(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths["latch_path"].write_bytes(_marker_bytes())

    for overrides, error_code in (
        ({"confirmation": "almost"}, "CONFIRMATION_MISMATCH"),
        ({"operator": "   "}, "OPERATOR_REQUIRED"),
        ({"reason": "   "}, "REASON_REQUIRED"),
    ):
        with pytest.raises(recovery.OfflineSafetyRecoveryError) as caught:
            _clear(tmp_path, **overrides)
        assert caught.value.error_code == error_code
        assert paths["latch_path"].read_bytes() == _marker_bytes()
        assert not paths["archive_dir"].exists()


def test_api_lock_must_be_exclusively_owned(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths["latch_path"].write_bytes(_marker_bytes())
    incumbent = SingleInstanceLock(paths["api_lock_path"], owner="live-api")
    incumbent.acquire()
    try:
        with pytest.raises(recovery.OfflineSafetyRecoveryError) as caught:
            _clear(tmp_path)
        assert caught.value.error_code == "API_PROCESS_LOCK_HELD"
        assert paths["latch_path"].read_bytes() == _marker_bytes()
    finally:
        incumbent.release()


def test_camera_lock_must_be_exclusively_owned_and_api_lock_is_released(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths["latch_path"].write_bytes(_marker_bytes())
    incumbent = SingleInstanceLock(
        paths["camera_worker_lock_path"], owner="orphan-camera-worker"
    )
    incumbent.acquire()
    try:
        with pytest.raises(recovery.OfflineSafetyRecoveryError) as caught:
            _clear(tmp_path)
        assert caught.value.error_code == "CAMERA_WORKER_PROCESS_LOCK_HELD"
        assert paths["latch_path"].read_bytes() == _marker_bytes()

        api_probe = SingleInstanceLock(paths["api_lock_path"], owner="api-probe")
        api_probe.acquire()
        api_probe.release()
    finally:
        incumbent.release()


def test_success_holds_both_locks_and_writes_byte_exact_archive_and_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    original_marker = _marker_bytes()
    paths["latch_path"].write_bytes(original_marker)
    original_reader = recovery._read_marker_bytes
    observations: list[tuple[bool, bool]] = []

    def observing_reader(path: Path) -> bytes:
        lock_results = []
        for lock_path in (paths["api_lock_path"], paths["camera_worker_lock_path"]):
            probe = SingleInstanceLock(lock_path, owner="exclusivity-probe")
            try:
                probe.acquire()
            except SingleWorkerGuardError:
                lock_results.append(True)
            else:
                lock_results.append(False)
                probe.release()
        observations.append((lock_results[0], lock_results[1]))
        return original_reader(path)

    monkeypatch.setattr(recovery, "_read_marker_bytes", observing_reader)
    receipt = _clear(tmp_path)

    assert observations and all(result == (True, True) for result in observations)
    assert receipt["status"] == "cleared"
    assert receipt["operator"] == "operator-001"
    assert receipt["reason"] == "physical stop and driver inspection completed"
    assert not paths["latch_path"].exists()

    archive_path = Path(receipt["archive_path"])
    receipt_path = Path(receipt["receipt_path"])
    assert archive_path.read_bytes() == original_marker
    persisted_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert persisted_receipt["status"] == "cleared"
    assert persisted_receipt["receipt_id"] == receipt["receipt_id"]
    assert persisted_receipt["original_marker"]["latch_id"] == "test-latch-id"

    # A successful tool exit does not retain either lock.
    api_probe = SingleInstanceLock(paths["api_lock_path"], owner="post-clear-api-probe")
    camera_probe = SingleInstanceLock(
        paths["camera_worker_lock_path"], owner="post-clear-camera-probe"
    )
    api_probe.acquire()
    camera_probe.acquire()
    camera_probe.release()
    api_probe.release()


def test_failed_atomic_move_keeps_active_marker_and_prepared_receipt(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    original_marker = _marker_bytes()
    paths["latch_path"].write_bytes(original_marker)

    with mock.patch.object(recovery.os, "replace", side_effect=OSError("injected")):
        with pytest.raises(recovery.OfflineSafetyRecoveryError) as caught:
            _clear(tmp_path)

    assert caught.value.error_code == "SAFETY_LATCH_CLEAR_FAILED"
    assert paths["latch_path"].read_bytes() == original_marker
    receipt_paths = list(paths["archive_dir"].glob("clearance_receipt-*.json"))
    archive_paths = list(paths["archive_dir"].glob("hardware_safety_latch-*.marker"))
    assert len(receipt_paths) == 1
    assert len(archive_paths) == 1
    assert json.loads(receipt_paths[0].read_text(encoding="utf-8"))["status"] == "prepared"
    assert archive_paths[0].read_bytes() == original_marker


def test_production_wrapper_has_no_caller_selected_paths() -> None:
    with mock.patch.object(
        recovery,
        "_clear_hardware_safety_latch_with_paths",
        return_value={"status": "cleared"},
    ) as implementation:
        recovery.clear_production_hardware_safety_latch(
            operator="operator-001",
            reason="verified safe",
            confirmation=recovery.CONFIRMATION_PHRASE,
        )

    called = implementation.call_args.kwargs
    assert called["latch_path"] == hardware_guard.PRODUCTION_HARDWARE_SAFETY_LATCH_PATH
    assert called["api_lock_path"] == recovery.PRODUCTION_API_LOCK_PATH
    assert "latch_path" not in (
        recovery.clear_production_hardware_safety_latch.__annotations__
    )


def test_private_reset_helper_refuses_production_safety_path() -> None:
    # conftest normally points tests at a temporary default. First forget any
    # initialized test path, then prove the immutable production path is denied.
    hardware_guard._reset_hardware_guard_for_tests(forget_persistence_path=True)
    original_default = hardware_guard.DEFAULT_HARDWARE_SAFETY_LATCH_PATH
    hardware_guard.DEFAULT_HARDWARE_SAFETY_LATCH_PATH = (
        hardware_guard.PRODUCTION_HARDWARE_SAFETY_LATCH_PATH
    )
    try:
        with pytest.raises(RuntimeError, match="refuses to modify production"):
            hardware_guard._reset_hardware_guard_for_tests(
                clear_persisted_latch=True,
                forget_persistence_path=True,
            )
    finally:
        hardware_guard.DEFAULT_HARDWARE_SAFETY_LATCH_PATH = original_default
        hardware_guard._reset_hardware_guard_for_tests(
            clear_persisted_latch=True,
            forget_persistence_path=True,
        )


def test_production_module_exposes_no_public_in_process_reset() -> None:
    assert not hasattr(hardware_guard, "reset_hardware_owners")
