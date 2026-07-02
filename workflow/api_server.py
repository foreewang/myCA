"""提供 Colony Workflow 的 FastAPI 服务入口并挂载任务、硬件、录像和产物查询接口。"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator, Dict

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from workflow.api_errors import (
    API_LOG_PATH,
    access_logger,
    api_error as _api_error,
    configure_api_file_logging as _configure_api_file_logging_base,
    error_detail as _error_detail,
    generic_http_message as _generic_http_message,
    hardware_guard_exception_handler as _hardware_guard_exception_handler,
    http_exception_handler as _http_exception_handler,
    is_public_error_detail as _is_public_error_detail,
    path_guard_exception_handler as _path_guard_exception_handler,
    register_api_error_handlers,
    request_validation_exception_handler as _request_validation_exception_handler,
    task_store_exception_handler as _task_store_exception_handler,
    unhandled_exception_handler as _unhandled_exception_handler,
)
from workflow.api_models import (
    CameraRecordStartRequest,
    ExecuteTaskRequest,
    StageReciprocationStartRequest,
    StageReciprocationStopRequest,
)
from workflow.camera_record_service import (
    CameraRecordServiceError,
    load_camera_settings_for_recording as _load_camera_settings_for_recording_base,
    start_camera_recording as _start_camera_recording,
    stop_camera_recording as _stop_camera_recording,
)
from workflow.file_io import read_json_with_retry
from workflow.hardware_guard import (
    STAGE_TERMINAL_STATUSES as _STAGE_TERMINAL_STATUSES,
    acquire_hardware_operation as _acquire_hardware_operation,
    current_hardware_owner as _current_hardware_owner,
    current_hardware_owners as _current_hardware_owners,
    release_hardware_operation as _release_hardware_operation,
)
from workflow.path_guard import (
    CONFIG_ROOT,
    DATA_ROOT,
    OUTPUTS_ROOT,
    PROJECT_ROOT,
)
from workflow.process_guard import SingleInstanceLock, assert_single_worker_config
from workflow.run_task import execute_task_request
from workflow.task_store import (
    TASK_ACTIVE_STATUSES as _TASK_ACTIVE_STATUSES,
    TASK_RECORD_IO_LOCK as _TASK_RECORD_IO_LOCK,
    TASK_TERMINAL_STATUSES as _TASK_TERMINAL_STATUSES,
    build_accepted_record as _build_accepted_record,
    build_failed_record as _build_failed_record,
    build_task_record as _build_task_record,
    mark_record_canceled as _mark_record_canceled,
    read_task_record as _read_task_record,
    read_task_record_unlocked as _read_task_record_unlocked,
    recover_interrupted_task_records as _recover_interrupted_task_records,
    sanitize_task_id as _sanitize_task_id,
    task_exists as _task_exists,
    update_task_record as _update_task_record,
    utc_now as _utc_now,
    write_task_record as _write_task_record,
    write_task_record_unlocked as _write_task_record_unlocked,
)
from workflow.task_runtime import (
    TaskRuntimeError,
    _TASK_CANCEL_EVENTS,
    _TASK_CANCEL_LOCK,
    cancel_task_request as _cancel_task_request,
    guess_current_progress as _guess_current_progress_base,
    is_task_cancel_requested as _is_task_cancel_requested_base,
    logger as task_runtime_logger,
    monitor_running_task as _monitor_running_task_base,
    normalize_execute_task_request as _normalize_execute_task_request_base,
    register_task_cancel_event as _register_task_cancel_event_base,
    request_task_cancel as _request_task_cancel_base,
    run_task_async as _run_task_async_base,
    start_task_runtime_manager as _start_task_runtime_manager,
    stop_monitor_thread as _stop_monitor_thread_base,
    stop_task_runtime_manager as _stop_task_runtime_manager,
    submit_task_request as _submit_task_request,
    unregister_task_cancel_event as _unregister_task_cancel_event_base,
)
from workflow.task_artifacts import (
    build_task_result_response as _build_task_result_response,
    build_well_images_response as _build_well_images_response,
    count_images as _count_images,
    ensure_well_record as _ensure_well_record,
    existing_output_path_or_none as _existing_output_path_or_none,
    resolve_image_dir as _resolve_image_dir,
    resolve_well_image_file as _resolve_well_image_file,
)

logger = logging.getLogger(__name__)


def _configure_api_file_logging() -> None:
    _configure_api_file_logging_base(extra_loggers=(logger, access_logger, task_runtime_logger))


_configure_api_file_logging()

@asynccontextmanager
async def _api_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    single_worker_lock: SingleInstanceLock | None = None
    try:
        configured_workers = assert_single_worker_config()
        if configured_workers is not None:
            logger.info("api worker configuration accepted: %s=%s", *configured_workers)
        single_worker_lock = SingleInstanceLock(
            PROJECT_ROOT / "data" / "api_server.lock",
            owner=f"pid={os.getpid()}",
        )
        single_worker_lock.acquire()
        logger.info("api single-worker lock acquired: %s", single_worker_lock.path)
        _recover_interrupted_task_records()
        _start_task_runtime_manager()
        yield
    finally:
        stopped = _stop_task_runtime_manager()
        if not stopped:
            logger.warning("task runtime worker did not stop within timeout")
        if single_worker_lock is not None:
            single_worker_lock.release()


app = FastAPI(title="Colony Workflow API", version="0.3.0", lifespan=_api_lifespan)
register_api_error_handlers(app)


@app.middleware("http")
async def log_request_timing(request, call_next):
    started = time.perf_counter()
    access_logger.info("request start: %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        access_logger.exception(
            "request failed: %s %s elapsed_ms=%.1f",
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    access_logger.info(
        "request end: %s %s status=%s elapsed_ms=%.1f",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


def _load_camera_settings_for_recording(req: CameraRecordStartRequest) -> Dict[str, Any]:
    return _load_camera_settings_for_recording_base(req)


def _normalize_execute_task_request(req: ExecuteTaskRequest) -> ExecuteTaskRequest:
    return _normalize_execute_task_request_base(req)


def _register_task_cancel_event(task_id: str, cancel_event: threading.Event) -> None:
    _register_task_cancel_event_base(task_id, cancel_event)


def _unregister_task_cancel_event(task_id: str) -> None:
    _unregister_task_cancel_event_base(task_id)


def _is_task_cancel_requested(task_id: str) -> bool:
    return _is_task_cancel_requested_base(task_id)


def _request_task_cancel(task_id: str) -> Dict[str, Any]:
    return _request_task_cancel_base(task_id)


def _guess_current_progress(record: Dict[str, Any]) -> Dict[str, Any]:
    return _guess_current_progress_base(record)


def _monitor_running_task(task_id: str, stop_event: threading.Event) -> None:
    _monitor_running_task_base(task_id, stop_event)


def _stop_monitor_thread(monitor: threading.Thread, stop_event: threading.Event, monitor_started: bool) -> None:
    _stop_monitor_thread_base(monitor, stop_event, monitor_started)


def _run_task_async(task: Dict[str, Any], req: ExecuteTaskRequest, cancel_event: threading.Event) -> None:
    _run_task_async_base(task, req, cancel_event, task_executor=execute_task_request)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/camera/record/status")
def get_camera_record_status() -> Dict[str, Any]:
    from workflow.camera_executor import recording_camera_status

    return recording_camera_status()


@app.get("/api/hardware/status")
def get_hardware_status() -> Dict[str, Any]:
    owners = _current_hardware_owners()
    return {
        "busy": bool(owners),
        "owners": owners,
        "owner": owners[0] if owners else {},
    }


@app.post("/api/camera/record/start")
def start_camera_record(req: CameraRecordStartRequest) -> Dict[str, Any]:
    try:
        return _start_camera_recording(req, settings_loader=_load_camera_settings_for_recording)
    except CameraRecordServiceError as exc:
        raise _api_error(
            exc.status_code,
            exc.error_code,
            exc.message,
            log_detail=exc.log_detail,
            exc=exc.cause if exc.log_exception else None,
        ) from exc


@app.post("/api/camera/record/stop")
def stop_camera_record() -> Dict[str, Any]:
    try:
        return _stop_camera_recording()
    except CameraRecordServiceError as exc:
        raise _api_error(
            exc.status_code,
            exc.error_code,
            exc.message,
            log_detail=exc.log_detail,
            exc=exc.cause if exc.log_exception else None,
        ) from exc


@app.post("/api/stage/reciprocation/start", status_code=202)
def start_stage_reciprocation(req: StageReciprocationStartRequest | None = None) -> Dict[str, Any]:
    from workflow.stage_reciprocation import stage_reciprocation_controller

    req = req or StageReciprocationStartRequest()
    cfg = req.model_dump()
    operation_id = "stage_reciprocation"
    _acquire_hardware_operation("stage_reciprocation", operation_id)
    try:
        return stage_reciprocation_controller.start(cfg)
    except Exception as exc:
        _release_hardware_operation("stage_reciprocation", operation_id)
        raise _api_error(
            409,
            "STAGE_RECIPROCATION_START_FAILED",
            "位移台往复运动启动失败，请检查位移台连接或参数",
            log_detail=str(exc),
            exc=exc,
        ) from exc


@app.post("/api/stage/reciprocation/stop")
def stop_stage_reciprocation(req: StageReciprocationStopRequest | None = None) -> Dict[str, Any]:
    from workflow.stage_reciprocation import stage_reciprocation_controller

    try:
        join_timeout_s = 5.0 if req is None else req.join_timeout_s
        result = stage_reciprocation_controller.stop(join_timeout_s=join_timeout_s)
        if result.get("status") in _STAGE_TERMINAL_STATUSES:
            _release_hardware_operation("stage_reciprocation", "stage_reciprocation")
        return result
    except Exception as exc:
        raise _api_error(
            409,
            "STAGE_RECIPROCATION_STOP_FAILED",
            "位移台往复运动停止失败，请检查位移台状态",
            log_detail=str(exc),
            exc=exc,
        ) from exc


@app.get("/api/stage/reciprocation/status")
def get_stage_reciprocation_status() -> Dict[str, Any]:
    from workflow.stage_reciprocation import stage_reciprocation_controller

    return stage_reciprocation_controller.status()


@app.post("/api/tasks/execute", status_code=202)
def execute_task(req: ExecuteTaskRequest) -> Dict[str, Any]:
    req = _normalize_execute_task_request(req)
    try:
        return _submit_task_request(req, task_executor=execute_task_request, access_logger=access_logger)
    except TaskRuntimeError as exc:
        raise _api_error(
            exc.status_code,
            exc.error_code,
            exc.message,
            log_detail=exc.log_detail,
        ) from exc


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> Dict[str, Any]:
    return _cancel_task_request(task_id)


@app.get("/api/tasks/{task_id}/status")
def get_task_status(task_id: str) -> Dict[str, Any]:
    record = _read_task_record(task_id)
    return {
        "task_id": record.get("task_id"),
        "status": record.get("status"),
        "task_type": record.get("task_type"),
        "observe_scope": record.get("observe_scope"),
        "plate_type": record.get("plate_type"),
        "objective_name": record.get("objective_name"),
        "progress": record.get("progress", 0),
        "message": record.get("message"),
        "current_stage": record.get("current_stage"),
        "current_well": record.get("current_well"),
        "created_at": record.get("created_at"),
        "started_at": record.get("started_at"),
        "updated_at": record.get("updated_at"),
        "finished_at": record.get("finished_at"),
        "stored_at_utc": record.get("stored_at_utc"),
        "result_json_path": record.get("result_json_path"),
        "error_code": record.get("error_code"),
        "error": record.get("error"),
        "cancel_requested": record.get("cancel_requested", False),
        "cancel_requested_at": record.get("cancel_requested_at"),
        "canceled_at": record.get("canceled_at"),
        "cancel_reason": record.get("cancel_reason"),
    }


@app.get("/api/tasks/{task_id}/result")
def get_task_result(task_id: str) -> Dict[str, Any]:
    record = _read_task_record(task_id)
    return _build_task_result_response(record, _TASK_ACTIVE_STATUSES, json_reader=read_json_with_retry)


@app.get("/api/tasks/{task_id}/wells/{well_name}/images")
def list_well_images(
    task_id: str,
    well_name: str,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int | None, Query(ge=1, le=1000)] = None,
) -> Dict[str, Any]:
    record = _read_task_record(task_id)
    effective_limit = int(page_size if page_size is not None else limit)
    effective_offset = int((page - 1) * effective_limit if page is not None else offset)
    return _build_well_images_response(record, well_name, limit=effective_limit, offset=effective_offset)


@app.get("/api/tasks/{task_id}/wells/{well_name}/images/{filename}")
def download_well_image(task_id: str, well_name: str, filename: str):
    record = _read_task_record(task_id)
    file_path, media_type = _resolve_well_image_file(record, well_name, filename)
    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=file_path.name,
    )


