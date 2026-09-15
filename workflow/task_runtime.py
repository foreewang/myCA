"""管理 API 普通任务的提交、后台执行、进度监控、取消和资源释放生命周期。"""
from __future__ import annotations

from workflow.task_logging import with_task_logging, current_request_id

import copy
import logging
import os
import queue
import threading
from time import perf_counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict

from workflow.api_models import ExecuteTaskRequest
from workflow.hardware_guard import acquire_hardware_operation, release_hardware_operation
from workflow.path_guard import normalize_execute_task_values
from workflow.run_task import execute_task_request as default_execute_task_request
from workflow.task_artifacts import count_images
from workflow.task_control import TaskCanceled
from workflow.task_store import (
    TASK_RECORD_IO_LOCK,
    TASK_TERMINAL_STATUSES,
    build_accepted_record,
    create_accepted_task_record_if_allowed,
    finalize_failed_record,
    finalize_success_record,
    mark_record_canceled,
    normalize_task_objective_alias,
    read_task_record,
    read_task_record_unlocked,
    sanitize_task_id,
    update_task_record,
    utc_now,
    write_task_record,
    write_task_record_unlocked,
)

_TASK_CANCEL_LOCK = threading.RLock()
_TASK_CANCEL_EVENTS: Dict[str, threading.Event] = {}
DEFAULT_TASK_QUEUE_MAXSIZE = 16
TASK_WORKER_STOP_TIMEOUT_S = 10.0

logger = logging.getLogger(__name__)


class TaskRuntimeError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        *,
        log_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.error_code = str(error_code)
        self.message = str(message)
        self.log_detail = log_detail


def normalize_execute_task_request(req: ExecuteTaskRequest) -> ExecuteTaskRequest:
    values = normalize_execute_task_values(
        task=normalize_task_objective_alias(req.task or {}),
        camera_path=req.camera_path,
        objectives_path=req.objectives_path,
        plates_path=req.plates_path,
        dump_json=req.dump_json,
    )
    return ExecuteTaskRequest(
        task=values["task"],
        camera_path=values["camera_path"],
        objectives_path=values["objectives_path"],
        plates_path=values["plates_path"],
        dump_json=values["dump_json"],
        persist_result=req.persist_result,
    )


def register_task_cancel_event(task_id: str, cancel_event: threading.Event) -> None:
    with _TASK_CANCEL_LOCK:
        _TASK_CANCEL_EVENTS[sanitize_task_id(task_id)] = cancel_event


def unregister_task_cancel_event(task_id: str) -> None:
    with _TASK_CANCEL_LOCK:
        _TASK_CANCEL_EVENTS.pop(sanitize_task_id(task_id), None)


def is_task_cancel_requested(task_id: str) -> bool:
    normalized = sanitize_task_id(task_id)
    with _TASK_CANCEL_LOCK:
        event = _TASK_CANCEL_EVENTS.get(normalized)
        if event is not None and event.is_set():
            return True
    try:
        record = read_task_record(normalized)
    except Exception:
        return False
    return bool(record.get("cancel_requested", False))


def request_task_cancel(task_id: str) -> Dict[str, Any]:
    normalized = sanitize_task_id(task_id)
    with _TASK_CANCEL_LOCK:
        event = _TASK_CANCEL_EVENTS.get(normalized)
        if event is not None:
            event.set()

    with TASK_RECORD_IO_LOCK:
        record = read_task_record_unlocked(normalized)
        if record.get("status") in TASK_TERMINAL_STATUSES:
            return record
        now = utc_now()
        record["cancel_requested"] = True
        record["cancel_requested_at"] = now
        record["updated_at"] = now
        record["message"] = "cancel requested; task will stop at the next safe checkpoint"
        write_task_record_unlocked(record)
        return record


def cancel_task_request(task_id: str) -> Dict[str, Any]:
    record = request_task_cancel(task_id)
    status = record.get("status")
    if status in TASK_TERMINAL_STATUSES:
        return {
            "task_id": record.get("task_id"),
            "status": status,
            "cancel_requested": bool(record.get("cancel_requested", False)),
            "message": "task is already terminal",
        }

    return {
        "task_id": record.get("task_id"),
        "status": "cancel_requested",
        "previous_status": status,
        "cancel_requested": True,
        "cancel_requested_at": record.get("cancel_requested_at"),
        "message": record.get("message"),
    }


def _coerce_record_progress(value: Any, *, top_level: bool) -> int:
    try:
        progress = int(round(float(value)))
    except Exception:
        progress = 0
    max_value = 99 if top_level else 100
    return max(0, min(max_value, progress))


def update_task_progress(
    task_id: str,
    stage: str,
    progress: int | float,
    well: str | None,
    message: str,
) -> Dict[str, Any]:
    normalized = sanitize_task_id(task_id)
    now = utc_now()
    top_progress = _coerce_record_progress(progress, top_level=True)
    well_progress = _coerce_record_progress(progress, top_level=False)

    with TASK_RECORD_IO_LOCK:
        record = read_task_record_unlocked(normalized)
        if record.get("status") in TASK_TERMINAL_STATUSES:
            return record

        existing_progress = _coerce_record_progress(record.get("progress", 0), top_level=True)
        record["progress"] = max(existing_progress, top_progress)
        record["progress_source"] = "executor"
        record["current_stage"] = str(stage)
        record["current_well"] = well
        record["message"] = str(message)
        record["updated_at"] = now

        wells = record.get("wells")
        if well and isinstance(wells, dict):
            item = dict(wells.get(well) or {})
            if item.get("status") not in TASK_TERMINAL_STATUSES:
                item["status"] = "running"
                item["progress"] = well_progress
                item["current_stage"] = str(stage)
                item["message"] = str(message)
                item["updated_at"] = now
            wells = dict(wells)
            wells[well] = item
            record["wells"] = wells

        write_task_record_unlocked(record)
        return record


def make_task_progress_callback(task_id: str) -> Callable[[str, int | float, str | None, str], None]:
    def _callback(stage: str, progress: int | float, well: str | None, message: str) -> None:
        try:
            update_task_progress(task_id, stage, progress, well, message)
        except Exception:
            logger.exception("task progress update failed: stage=%s", stage, extra={"task_id": task_id})

    return _callback


def guess_current_progress(record: Dict[str, Any]) -> Dict[str, Any]:
    task_type = str(record.get("task_type") or "").lower()
    stage = "capture"
    message = "running"
    progress = max(int(record.get("progress") or 0), 1)
    current_well = record.get("current_well")

    wells = record.get("wells") or {}
    if wells:
        total_imgs = 0
        first_active = None
        detect_done = 0
        for well_name, meta in wells.items():
            image_dir = Path(meta["image_dir"]) if meta.get("image_dir") else None
            img_count = count_images(image_dir) if image_dir else 0
            total_imgs += img_count
            if img_count > 0 and first_active is None:
                first_active = well_name
            detect_json = meta.get("detect_result_json")
            if detect_json and Path(detect_json).exists():
                detect_done += 1

        current_well = first_active or current_well
        if task_type == "pipeline":
            if detect_done > 0:
                stage = "detect"
                progress = max(progress, 80 if detect_done < len(wells) else 95)
                message = f"detecting, wells done={detect_done}/{len(wells)}"
            else:
                stage = "capture"
                progress = max(progress, 10 if total_imgs == 0 else min(70, 10 + total_imgs))
                message = f"capturing, images saved={total_imgs}"
        elif task_type == "capture":
            stage = "capture"
            progress = max(progress, 10 if total_imgs == 0 else min(95, 10 + total_imgs))
            message = f"capturing, images saved={total_imgs}"
        elif task_type == "compensate":
            stage = "compensate"
            progress = max(progress, 50)
            message = "compensating"
    else:
        if task_type == "compensate":
            stage = "compensate"
            progress = max(progress, 50)
            message = "compensating"

    return {
        "status": "running",
        "progress": progress,
        "message": message,
        "current_stage": stage,
        "current_well": current_well,
    }


@with_task_logging("task_id")
def monitor_running_task(task_id: str, stop_event: threading.Event, *, request_id: str | None = None) -> None:
    while not stop_event.is_set():
        try:
            record = read_task_record(task_id)
            if record.get("status") in TASK_TERMINAL_STATUSES:
                return
            if record.get("cancel_requested"):
                return
            if record.get("progress_source") == "executor":
                stop_event.wait(1.0)
                continue
            patch = guess_current_progress(record)
            patch["progress_source"] = "fallback"
            update_task_record(task_id, patch)
        except Exception:
            logger.exception("monitor task failed")
        stop_event.wait(1.0)


def stop_monitor_thread(monitor: threading.Thread, stop_event: threading.Event, monitor_started: bool) -> None:
    stop_event.set()
    if monitor_started:
        monitor.join(timeout=1.0)


TaskExecutor = Callable[..., Dict[str, Any]]


@dataclass
class QueuedTask:
    task: Dict[str, Any]
    req: ExecuteTaskRequest
    cancel_event: threading.Event
    task_executor: TaskExecutor
    request_id: str = "-"


@with_task_logging("task")
def run_task_async(
    task: Dict[str, Any],
    req: ExecuteTaskRequest,
    cancel_event: threading.Event,
    *,
    task_executor: TaskExecutor = default_execute_task_request,
    request_id: str | None = None,
) -> None:
    task_id = str(task.get("task_id") or "").strip()
    started = perf_counter()
    logger.info("event=task_started")
    stop_event = threading.Event()
    monitor = threading.Thread(target=monitor_running_task, args=(task_id, stop_event),
                               kwargs={"request_id": current_request_id.get()}, daemon=False)
    monitor_started = False
    try:
        update_task_record(
            task_id,
            {"status": "running", "started_at": utc_now(), "message": "task started", "progress": 1},
        )
        monitor.start()
        monitor_started = True
        result = task_executor(
            raw_task_cfg={"task": task},
            camera_path=req.camera_path or os.getenv("CAMERA_CONFIG_PATH"),
            objectives_path=req.objectives_path or os.getenv("OBJECTIVES_CONFIG_PATH"),
            plates_path=req.plates_path or os.getenv("PLATES_CONFIG_PATH"),
            dump_json=req.dump_json,
            persist_result=req.persist_result,
            cancel_check=lambda: cancel_event.is_set() or is_task_cancel_requested(task_id),
            progress_callback=make_task_progress_callback(task_id),
        )
        stop_monitor_thread(monitor, stop_event, monitor_started)
        monitor_started = False
        try:
            existing_record = read_task_record(task_id)
        except Exception:
            existing_record = build_accepted_record(task, req.dump_json, req.persist_result)
        record = finalize_success_record(existing_record, task, result, req.dump_json, req.persist_result)
        write_task_record(record)
        logger.info("event=task_completed elapsed_ms=%.1f", (perf_counter() - started) * 1000)
    except TaskCanceled as exc:
        logger.info("event=task_canceled elapsed_ms=%.1f", (perf_counter() - started) * 1000)
        stop_monitor_thread(monitor, stop_event, monitor_started)
        monitor_started = False
        try:
            record = read_task_record(task_id)
        except Exception:
            record = build_accepted_record(task, req.dump_json, req.persist_result)
        write_task_record(mark_record_canceled(record, str(exc)))
    except Exception:
        logger.exception("event=task_failed error_code=TASK_EXECUTION_FAILED elapsed_ms=%.1f", (perf_counter() - started) * 1000)
        stop_monitor_thread(monitor, stop_event, monitor_started)
        monitor_started = False
        try:
            existing_record = read_task_record(task_id)
        except Exception:
            existing_record = build_accepted_record(task, req.dump_json, req.persist_result)
        record = finalize_failed_record(
            existing_record,
            task,
            "任务执行失败，请查看本地日志或联系维护人员",
            req.dump_json,
            req.persist_result,
        )
        write_task_record(record)
    finally:
        stop_monitor_thread(monitor, stop_event, monitor_started)
        unregister_task_cancel_event(task_id)
        release_hardware_operation("task", task_id)


class TaskRuntimeManager:
    def __init__(self, maxsize: int = DEFAULT_TASK_QUEUE_MAXSIZE) -> None:
        self.maxsize = max(1, int(maxsize))
        self._queue: queue.Queue[QueuedTask | None] = queue.Queue(maxsize=self.maxsize)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._accepting = False

    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                self._accepting = True
                return
            self._stop_event.clear()
            self._accepting = True
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="TaskRuntimeWorker",
                daemon=False,
            )
            self._worker.start()

    def stop(self, timeout_s: float = TASK_WORKER_STOP_TIMEOUT_S) -> bool:
        with self._lock:
            self._accepting = False
            self._stop_event.set()
            worker = self._worker
            if worker is None:
                return True
            if worker.is_alive():
                try:
                    self._queue.put_nowait(None)
                except queue.Full:
                    pass

        worker.join(timeout=max(0.0, float(timeout_s)))
        stopped = not worker.is_alive()
        self._discard_pending_items()
        if stopped:
            with self._lock:
                if self._worker is worker:
                    self._worker = None
        return stopped

    def is_running(self) -> bool:
        with self._lock:
            return self._worker is not None and self._worker.is_alive() and self._accepting

    @with_task_logging("req")
    def submit(
        self,
        req: ExecuteTaskRequest,
        *,
        task_executor: TaskExecutor = default_execute_task_request,
        access_logger: logging.Logger | None = None,
    ) -> Dict[str, Any]:
        task = req.task or {}
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            raise TaskRuntimeError(400, "TASK_ID_REQUIRED", "任务 ID 不能为空")

        with self._lock:
            if not self._accepting or self._worker is None or not self._worker.is_alive():
                raise TaskRuntimeError(
                    503,
                    "TASK_RUNTIME_NOT_RUNNING",
                    "任务运行队列未启动，请检查服务状态",
                )
            if self._queue.full():
                raise TaskRuntimeError(
                    429,
                    "TASK_QUEUE_FULL",
                    "任务队列已满，请稍后重试",
                    log_detail=f"task_id={task_id} maxsize={self.maxsize}",
                )

            cancel_event = threading.Event()
            register_task_cancel_event(task_id, cancel_event)
            try:
                record = create_accepted_task_record_if_allowed(task, req.dump_json, req.persist_result)
                self._queue.put_nowait(
                    QueuedTask(
                        task=copy.deepcopy(task),
                        req=req,
                        cancel_event=cancel_event,
                        task_executor=task_executor,
                        request_id=current_request_id.get(),
                    )
                )
                logger.info("event=task_queued queue_size=%s", self._queue.qsize(), extra={"task_id": task_id})
            except queue.Full as exc:
                unregister_task_cancel_event(task_id)
                raise TaskRuntimeError(
                    429,
                    "TASK_QUEUE_FULL",
                    "任务队列已满，请稍后重试",
                    log_detail=f"task_id={task_id} maxsize={self.maxsize}",
                ) from exc
            except Exception:
                unregister_task_cancel_event(task_id)
                raise

        return {
            "task_id": task_id,
            "status": "accepted",
            "task_type": task.get("task_type"),
            "observe_scope": task.get("observe_scope"),
            "objective_name": record.get("objective_name"),
            "message": "task accepted",
            "result_json_path": record.get("result_json_path"),
        }

    def _worker_loop(self) -> None:
        while True:
            if self._stop_event.is_set():
                return
            item = self._queue.get()
            try:
                if item is None:
                    return
                # Wait for submit to finish recording admission before execution.
                with self._lock:
                    pass
                if item.cancel_event.is_set() or is_task_cancel_requested(str(item.task.get("task_id") or "")):
                    self._mark_queued_task_canceled(item, "operator canceled before task started")
                    continue
                self._execute_queued_task(item)
            finally:
                self._queue.task_done()

    def _discard_pending_items(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                if item is not None:
                    unregister_task_cancel_event(str(item.task.get("task_id") or ""))
                    logger.warning("event=task_deferred reason=runtime_shutdown",
                                   extra={"task_id": item.task.get("task_id") or "-",
                                          "request_id": item.request_id})
            finally:
                self._queue.task_done()

    @with_task_logging("item")
    def _execute_queued_task(self, item: QueuedTask) -> None:
        task_id = str(item.task.get("task_id") or "").strip()
        try:
            acquire_hardware_operation("task", task_id)
        except Exception as exc:
            logger.exception("event=task_admission_failed error_code=HARDWARE_BUSY",
                             extra={"task_id": task_id, "request_id": item.request_id})
            self._mark_queued_task_failed(item, "硬件正在执行其他任务，请稍后重试", "HARDWARE_BUSY")
            unregister_task_cancel_event(task_id)
            return
        run_task_async(item.task, item.req, item.cancel_event, task_executor=item.task_executor, request_id=item.request_id)

    @with_task_logging("item")
    def _mark_queued_task_canceled(self, item: QueuedTask, reason: str) -> None:
        task_id = str(item.task.get("task_id") or "").strip()
        try:
            record = read_task_record(task_id)
        except Exception:
            record = build_accepted_record(item.task, item.req.dump_json, item.req.persist_result)
        write_task_record(mark_record_canceled(record, reason))
        logger.info("event=task_canceled reason=before_start", extra={"task_id": task_id, "request_id": item.request_id})
        unregister_task_cancel_event(task_id)

    @with_task_logging("item")
    def _mark_queued_task_failed(self, item: QueuedTask, error: str, error_code: str) -> None:
        task_id = str(item.task.get("task_id") or "").strip()
        try:
            existing_record = read_task_record(task_id)
        except Exception:
            existing_record = build_accepted_record(item.task, item.req.dump_json, item.req.persist_result)
        record = finalize_failed_record(
            existing_record,
            item.task,
            error,
            item.req.dump_json,
            item.req.persist_result,
            error_code=error_code,
        )
        write_task_record(record)

        logger.error("event=task_failed error_code=%s", error_code,
                     extra={"task_id": task_id, "request_id": item.request_id})


DEFAULT_TASK_RUNTIME_MANAGER = TaskRuntimeManager()


def start_task_runtime_manager() -> None:
    DEFAULT_TASK_RUNTIME_MANAGER.start()


def stop_task_runtime_manager(timeout_s: float = TASK_WORKER_STOP_TIMEOUT_S) -> bool:
    return DEFAULT_TASK_RUNTIME_MANAGER.stop(timeout_s=timeout_s)


def submit_task_request(
    req: ExecuteTaskRequest,
    *,
    task_executor: TaskExecutor = default_execute_task_request,
    access_logger: logging.Logger | None = None,
    manager: TaskRuntimeManager | None = None,
) -> Dict[str, Any]:
    runtime_manager = manager or DEFAULT_TASK_RUNTIME_MANAGER
    return runtime_manager.submit(req, task_executor=task_executor, access_logger=access_logger)
