"""Fail-closed task-queue shutdown regressions."""
from __future__ import annotations

import threading

from workflow import hardware_guard, task_runtime, task_store
from workflow.api_models import ExecuteTaskRequest


def test_shutdown_request_does_not_wait_for_submit_lock() -> None:
    manager = task_runtime.TaskRuntimeManager(maxsize=1)
    manager._accepting = True
    lock_held = threading.Event()
    release_lock = threading.Event()
    request_done = threading.Event()

    def hold_submit_lock() -> None:
        with manager._lock:
            lock_held.set()
            release_lock.wait(timeout=2)

    holder = threading.Thread(target=hold_submit_lock)
    requester = threading.Thread(
        target=lambda: (manager.request_stop(), request_done.set()),
    )
    holder.start()
    try:
        assert lock_held.wait(timeout=1)
        requester.start()
        assert request_done.wait(timeout=0.5)
        assert manager._stop_event.is_set()
        assert manager._accepting is False
    finally:
        release_lock.set()
        holder.join(timeout=1)
        requester.join(timeout=1)


def test_shutdown_marks_never_started_queue_items_canceled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path))
    hardware_guard._reset_hardware_guard_for_tests()
    manager = task_runtime.TaskRuntimeManager(maxsize=2)
    entered = threading.Event()
    release = threading.Event()

    def blocking_executor(*, raw_task_cfg, cancel_check, **_kwargs):
        entered.set()
        release.wait(timeout=5)
        if cancel_check():
            raise task_runtime.TaskCanceled("runtime shutdown requested")
        return {
            "status": "success",
            "task_id": raw_task_cfg["task"]["task_id"],
            "task_type": "capture",
        }

    manager.start()
    try:
        manager.submit(
            ExecuteTaskRequest(task={"task_id": "active-at-shutdown", "task_type": "capture"}),
            task_executor=blocking_executor,
        )
        assert entered.wait(timeout=2)
        manager.submit(
            ExecuteTaskRequest(task={"task_id": "queued-at-shutdown", "task_type": "capture"}),
            task_executor=blocking_executor,
        )

        assert manager.stop(timeout_s=0.01) is False
        queued = task_store.read_task_record("queued-at-shutdown")
        assert queued["status"] == "canceled"
        assert queued["cancel_requested"] is True
        assert "shutdown" in queued["cancel_reason"].lower()
    finally:
        release.set()
        assert manager.wait_until_quiescent(timeout_s=2)
        hardware_guard._reset_hardware_guard_for_tests()
