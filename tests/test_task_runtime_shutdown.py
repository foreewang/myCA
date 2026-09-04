"""Fail-closed task-queue shutdown regressions."""
from __future__ import annotations

import threading
import time
from unittest import mock

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


def test_stop_timeout_includes_stalled_queue_record_cleanup() -> None:
    manager = task_runtime.TaskRuntimeManager(maxsize=2)
    entered = threading.Event()
    release = threading.Event()
    queued = task_runtime.QueuedTask(
        task={"task_id": "stalled-cleanup", "task_type": "capture"},
        req=ExecuteTaskRequest(
            task={"task_id": "stalled-cleanup", "task_type": "capture"}
        ),
        cancel_event=threading.Event(),
        task_executor=lambda **_kwargs: {},
    )
    manager._queue.put_nowait(queued)

    def stalled_record_cleanup(_item, _reason) -> None:
        entered.set()
        release.wait(timeout=2.0)

    try:
        with mock.patch.object(
            manager,
            "_mark_queued_task_canceled",
            side_effect=stalled_record_cleanup,
        ):
            started = time.monotonic()
            assert manager.stop(timeout_s=0.05) is False
            elapsed = time.monotonic() - started
            assert elapsed < 0.5
            assert entered.is_set()
            assert manager.is_quiescent() is False
            release.set()
            assert manager.wait_until_quiescent(timeout_s=2.0)
    finally:
        release.set()


def test_shutdown_cleanup_waits_for_submission_that_passed_final_admission_check(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path))
    hardware_guard._reset_hardware_guard_for_tests()
    manager = task_runtime.TaskRuntimeManager(maxsize=2)
    enqueue_entered = threading.Event()
    release_enqueue = threading.Event()
    submit_result = {}
    submit_errors = []
    real_put_nowait = manager._queue.put_nowait

    def gated_put_nowait(item) -> None:
        if isinstance(item, task_runtime.QueuedTask):
            enqueue_entered.set()
            release_enqueue.wait(timeout=2.0)
        real_put_nowait(item)

    def submit() -> None:
        try:
            submit_result.update(
                manager.submit(
                    ExecuteTaskRequest(
                        task={"task_id": "late-queue-admission", "task_type": "capture"}
                    ),
                    task_executor=lambda **_kwargs: {},
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            submit_errors.append(exc)

    manager.start()
    submitter = threading.Thread(target=submit)
    try:
        with mock.patch.object(manager._queue, "put_nowait", side_effect=gated_put_nowait):
            submitter.start()
            assert enqueue_entered.wait(timeout=1.0)

            assert manager.stop(timeout_s=0.05) is False
            assert manager._shutdown_cleanup_thread is not None
            assert manager._shutdown_cleanup_thread.is_alive()

            release_enqueue.set()
            submitter.join(timeout=1.0)
            assert not submitter.is_alive()
            assert submit_errors == []
            assert submit_result["status"] == "accepted"
            assert manager.wait_until_quiescent(timeout_s=2.0)

        record = task_store.read_task_record("late-queue-admission")
        assert record["status"] == "canceled"
        assert record["cancel_requested"] is True
        assert manager._queue.empty()
    finally:
        release_enqueue.set()
        submitter.join(timeout=1.0)
        manager.stop(timeout_s=1.0)
        hardware_guard._reset_hardware_guard_for_tests()


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
