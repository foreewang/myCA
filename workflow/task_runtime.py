"""管理 API 普通任务的提交、后台执行、进度监控、取消和资源释放生命周期。"""
from __future__ import annotations

import copy
import logging
import os
import threading
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
    build_failed_record,
    build_task_record,
    create_accepted_task_record_if_allowed,
    mark_record_canceled,
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
        task=req.task or {},
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


def monitor_running_task(task_id: str, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            record = read_task_record(task_id)
            if record.get("status") in TASK_TERMINAL_STATUSES:
                return
            if record.get("cancel_requested"):
                return
            patch = guess_current_progress(record)
            update_task_record(task_id, patch)
        except Exception:
            logger.exception("monitor task failed: task_id=%s", task_id)
        stop_event.wait(1.0)


def stop_monitor_thread(monitor: threading.Thread, stop_event: threading.Event, monitor_started: bool) -> None:
    stop_event.set()
    if monitor_started:
        monitor.join(timeout=1.0)


TaskExecutor = Callable[..., Dict[str, Any]]


def run_task_async(
    task: Dict[str, Any],
    req: ExecuteTaskRequest,
    cancel_event: threading.Event,
    *,
    task_executor: TaskExecutor = default_execute_task_request,
) -> None:
    task_id = str(task.get("task_id") or "").strip()
    stop_event = threading.Event()
    monitor = threading.Thread(target=monitor_running_task, args=(task_id, stop_event), daemon=True)
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
        )
        stop_monitor_thread(monitor, stop_event, monitor_started)
        monitor_started = False
        record = build_task_record(task, result, req.dump_json, req.persist_result)
        write_task_record(record)
    except TaskCanceled as exc:
        logger.info("task canceled: %s", task_id)
        stop_monitor_thread(monitor, stop_event, monitor_started)
        monitor_started = False
        try:
            record = read_task_record(task_id)
        except Exception:
            record = build_accepted_record(task, req.dump_json, req.persist_result)
        write_task_record(mark_record_canceled(record, str(exc)))
    except Exception:
        logger.exception("task execution failed: %s", task_id)
        stop_monitor_thread(monitor, stop_event, monitor_started)
        monitor_started = False
        record = build_failed_record(
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


def submit_task_request(
    req: ExecuteTaskRequest,
    *,
    task_executor: TaskExecutor = default_execute_task_request,
    access_logger: logging.Logger | None = None,
) -> Dict[str, Any]:
    task = req.task or {}
    task_id = str(task.get("task_id") or "").strip()
    if access_logger is not None:
        access_logger.info("execute_task entered: task_id=%s", task_id or "<empty>")
    if not task_id:
        raise TaskRuntimeError(400, "TASK_ID_REQUIRED", "任务 ID 不能为空")

    acquire_hardware_operation("task", task_id)
    cancel_event = threading.Event()
    register_task_cancel_event(task_id, cancel_event)
    try:
        record = create_accepted_task_record_if_allowed(task, req.dump_json, req.persist_result)
        if access_logger is not None:
            access_logger.info("execute_task writing accepted record: task_id=%s", task_id)

        worker = threading.Thread(
            target=run_task_async,
            args=(copy.deepcopy(task), req, cancel_event),
            kwargs={"task_executor": task_executor},
            daemon=True,
        )
        worker.start()
    except Exception:
        unregister_task_cancel_event(task_id)
        release_hardware_operation("task", task_id)
        raise

    if access_logger is not None:
        access_logger.info("execute_task worker started: task_id=%s thread=%s", task_id, worker.name)

    return {
        "task_id": task_id,
        "status": "accepted",
        "task_type": task.get("task_type"),
        "observe_scope": task.get("observe_scope"),
        "message": "task accepted",
        "result_json_path": record.get("result_json_path"),
    }
