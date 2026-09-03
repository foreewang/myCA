"""提供 Colony Workflow 的 FastAPI 服务入口并挂载任务、硬件、录像和产物查询接口。"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator, Callable, Dict

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse

from workflow.api_errors import (
    access_logger,
    api_error as _api_error,
    configure_api_file_logging as _configure_api_file_logging_base,
    register_api_error_handlers,
)
from workflow.api_models import (
    CameraRecordStopRequest,
    CameraRecordStartRequest,
    ExecuteTaskRequest,
    StageReciprocationStartRequest,
    StageReciprocationStopRequest,
)
from workflow.camera_record_service import (
    CameraRecordServiceError,
    start_camera_recording,
    stop_camera_recording,
)
from workflow.file_io import read_json_with_retry
from workflow.hardware_guard import (
    HardwareGuardError,
    STAGE_TERMINAL_STATUSES,
    acquire_hardware_operation,
    close_hardware_admission_for_shutdown,
    confirm_hardware_operation,
    current_hardware_owners,
    hardware_safety_shutdown_fence_ready,
    hardware_safety_status,
    open_hardware_admission_after_startup,
    release_hardware_operation,
    restore_persisted_hardware_safety_latch,
)
from workflow.path_guard import PROJECT_ROOT
from workflow.process_guard import SingleInstanceLock, assert_single_worker_config
from workflow.task_store import (
    TASK_ACTIVE_STATUSES,
    read_task_record,
    recover_interrupted_task_records,
)
from workflow.task_runtime import (
    TaskRuntimeError,
    cancel_task_request,
    logger as task_runtime_logger,
    normalize_execute_task_request,
    request_task_runtime_shutdown,
    start_task_runtime_manager,
    stop_task_runtime_manager,
    submit_task_request,
    task_runtime_manager_is_quiescent,
)
from workflow.task_artifacts import (
    build_task_result_response,
    build_well_images_response,
    resolve_well_image_file,
)

logger = logging.getLogger(__name__)
camera_supervisor_logger = logging.getLogger("workflow.camera_process_supervisor")
_RETAINED_INSTANCE_LOCKS_LOCK = threading.RLock()
_RETAINED_INSTANCE_LOCKS: list[SingleInstanceLock] = []


def _configure_api_file_logging() -> None:
    _configure_api_file_logging_base(
        extra_loggers=(logger, access_logger, task_runtime_logger, camera_supervisor_logger)
    )


_configure_api_file_logging()


async def _await_thread_cleanup(
    call: Callable[[], Any],
) -> tuple[Any, asyncio.CancelledError | None, BaseException | None]:
    """Run bounded blocking cleanup to completion without losing cancellation.

    ``asyncio.to_thread`` does not stop its worker when the awaiting task is
    cancelled.  Shielding and then waiting for the bounded cleanup prevents a
    half-shutdown state (camera admission still open or the API lock retained),
    while returning the cancellation so the caller can re-raise it afterwards.
    """

    task = asyncio.create_task(asyncio.to_thread(call))
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
        except BaseException:
            break
    try:
        return task.result(), cancellation, None
    except BaseException as exc:
        return None, cancellation, exc


def _log_cleanup_error(message: str, exc: BaseException) -> None:
    logger.error(
        "%s: %s",
        message,
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )


def _managed_runtime_is_quiescent() -> bool:
    from workflow.camera_process_supervisor import camera_process_supervisors_are_quiescent
    from workflow.stage_reciprocation import stage_reciprocation_is_quiescent

    return bool(
        task_runtime_manager_is_quiescent()
        and stage_reciprocation_is_quiescent()
        and camera_process_supervisors_are_quiescent()
        and hardware_safety_shutdown_fence_ready()
    )


def _request_stage_runtime_shutdown() -> None:
    from workflow.stage_reciprocation import request_stage_reciprocation_shutdown

    request_stage_reciprocation_shutdown()


def _request_camera_runtime_shutdown() -> None:
    from workflow.camera_process_supervisor import request_camera_process_supervisor_shutdown

    request_camera_process_supervisor_shutdown()


def _retain_instance_lock_until_quiescent(lock: SingleInstanceLock) -> None:
    """Keep the cross-process generation fence while old work can still act."""

    with _RETAINED_INSTANCE_LOCKS_LOCK:
        _RETAINED_INSTANCE_LOCKS.append(lock)

    def release_when_safe() -> None:
        quiescence_error_logged = False
        while True:
            try:
                if _managed_runtime_is_quiescent():
                    break
            except BaseException as exc:
                if not quiescence_error_logged:
                    _log_cleanup_error("failed to evaluate retained runtime quiescence", exc)
                    quiescence_error_logged = True
            time.sleep(0.05)
        try:
            lock.release()
        except BaseException as exc:
            _log_cleanup_error("failed to release retained API single-worker lock", exc)
            return
        with _RETAINED_INSTANCE_LOCKS_LOCK:
            if lock in _RETAINED_INSTANCE_LOCKS:
                _RETAINED_INSTANCE_LOCKS.remove(lock)

    watcher = threading.Thread(
        target=release_when_safe,
        name="ApiGenerationLockDrain",
        # Keeping this watcher non-daemon makes the retained generation fence
        # survive a failed graceful shutdown.  The service manager may kill the
        # old process only after operators have made the hardware safe.
        daemon=False,
    )
    watcher.start()


@asynccontextmanager
async def _api_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    single_worker_lock: SingleInstanceLock | None = None
    generation_owned = False
    camera_supervisor_initialized = False
    stage_controller_initialized = False
    task_runtime_cleanup_required = False
    body_error: BaseException | None = None
    try:
        configured_workers = assert_single_worker_config()
        if configured_workers is not None:
            logger.info("api worker configuration accepted: %s=%s", *configured_workers)
        single_worker_lock = SingleInstanceLock(
            PROJECT_ROOT / "data" / "api_server.lock",
            owner=f"pid={os.getpid()}",
        )
        single_worker_lock.acquire()
        generation_owned = True
        logger.info("api single-worker lock acquired: %s", single_worker_lock.path)
        close_hardware_admission_for_shutdown()
        restore_persisted_hardware_safety_latch()
        recover_interrupted_task_records()
        # Validate all camera-process timeout environment variables during
        # lifespan startup.  This creates only the local supervisor; the MVS
        # child process remains lazy until the first camera operation.
        from workflow.camera_process_supervisor import initialize_camera_process_supervisor
        from workflow.stage_reciprocation import initialize_stage_reciprocation_controller

        initialize_camera_process_supervisor()
        camera_supervisor_initialized = True
        initialize_stage_reciprocation_controller()
        stage_controller_initialized = True
        # If start fails after partially creating a worker, this generation is
        # still responsible for the bounded cleanup in finally.
        task_runtime_cleanup_required = True
        start_task_runtime_manager()
        open_hardware_admission_after_startup()
        yield
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        cleanup_cancellation: asyncio.CancelledError | None = None

        # Phase one publishes every cancellation/stop gate before any join or
        # native shutdown call.  A stalled task-record write must not delay the
        # stage or camera from observing shutdown.
        if generation_owned:
            try:
                close_hardware_admission_for_shutdown()
            except BaseException as exc:
                cleanup_error = exc
                _log_cleanup_error("failed to close hardware admission during API shutdown", exc)

            shutdown_requests: tuple[tuple[bool, Callable[[], Any], str], ...] = (
                (
                    task_runtime_cleanup_required,
                    request_task_runtime_shutdown,
                    "failed to publish task runtime shutdown",
                ),
                (
                    stage_controller_initialized,
                    _request_stage_runtime_shutdown,
                    "failed to publish stage runtime shutdown",
                ),
                (
                    camera_supervisor_initialized,
                    _request_camera_runtime_shutdown,
                    "failed to publish camera runtime shutdown",
                ),
            )
            for enabled, request_shutdown, failure_message in shutdown_requests:
                if not enabled:
                    continue
                try:
                    request_shutdown()
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
                    _log_cleanup_error(failure_message, exc)

        # Phase two performs bounded joins, native stop/finalization, and only
        # then permits the generation marker/API lock to be released.
        try:
            if task_runtime_cleanup_required:
                try:
                    stopped, cancellation, error = await _await_thread_cleanup(
                        stop_task_runtime_manager
                    )
                    if cancellation is not None:
                        cleanup_cancellation = cancellation
                    if error is not None:
                        if isinstance(error, asyncio.CancelledError):
                            cleanup_cancellation = cleanup_cancellation or error
                        else:
                            cleanup_error = error
                            _log_cleanup_error(
                                "failed to stop task runtime during API shutdown", error
                            )
                    elif not stopped:
                        logger.warning("task runtime worker did not stop within timeout")
                except asyncio.CancelledError as exc:
                    cleanup_cancellation = cleanup_cancellation or exc
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
                    _log_cleanup_error("failed to schedule task runtime shutdown", exc)

            if stage_controller_initialized:
                try:
                    from workflow.stage_reciprocation import shutdown_stage_reciprocation_controller

                    stage_stopped, cancellation, error = await _await_thread_cleanup(
                        shutdown_stage_reciprocation_controller
                    )
                    if cancellation is not None:
                        cleanup_cancellation = cleanup_cancellation or cancellation
                    if error is not None:
                        if isinstance(error, asyncio.CancelledError):
                            cleanup_cancellation = cleanup_cancellation or error
                        else:
                            cleanup_error = cleanup_error or error
                            _log_cleanup_error("failed to stop stage reciprocation during API shutdown", error)
                    elif not stage_stopped:
                        logger.error("stage reciprocation did not stop within timeout")
                    else:
                        release_hardware_operation("stage_reciprocation", "stage_reciprocation")
                except asyncio.CancelledError as exc:
                    cleanup_cancellation = cleanup_cancellation or exc
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
                    _log_cleanup_error("failed to schedule stage reciprocation shutdown", exc)

            if camera_supervisor_initialized:
                try:
                    from workflow.camera_executor import shutdown_recording_camera

                    _, cancellation, error = await _await_thread_cleanup(shutdown_recording_camera)
                    if cancellation is not None:
                        cleanup_cancellation = cleanup_cancellation or cancellation
                    if error is not None:
                        if isinstance(error, asyncio.CancelledError):
                            cleanup_cancellation = cleanup_cancellation or error
                        else:
                            _log_cleanup_error("failed to stop camera recording during API shutdown", error)
                except asyncio.CancelledError as exc:
                    cleanup_cancellation = cleanup_cancellation or exc
                except BaseException as exc:
                    _log_cleanup_error("failed to schedule camera shutdown", exc)
        finally:
            if generation_owned and single_worker_lock is not None:
                try:
                    managed_runtime_quiescent = _managed_runtime_is_quiescent()
                except BaseException as exc:
                    managed_runtime_quiescent = False
                    cleanup_error = cleanup_error or exc
                    _log_cleanup_error("failed to evaluate managed runtime quiescence", exc)
                if managed_runtime_quiescent:
                    try:
                        single_worker_lock.release()
                    except BaseException as exc:
                        cleanup_error = cleanup_error or exc
                        _log_cleanup_error("failed to release API single-worker lock", exc)
                else:
                    try:
                        _retain_instance_lock_until_quiescent(single_worker_lock)
                    except BaseException as exc:
                        cleanup_error = cleanup_error or exc
                        _log_cleanup_error("failed to retain API generation lock", exc)
                    cleanup_error = cleanup_error or RuntimeError(
                        "managed hardware work outlived API shutdown; generation lock retained"
                    )
                    logger.critical(
                        "managed hardware work outlived API shutdown; refusing to release generation lock"
                    )

        # Preserve an exception raised by startup or the lifespan body.  On a
        # normal exit, cancellation has priority and is deliberately re-raised
        # only after every cleanup stage and the cross-process lock release.
        if body_error is None:
            if cleanup_cancellation is not None:
                raise cleanup_cancellation
            if cleanup_error is not None:
                raise cleanup_error


app = FastAPI(title="Colony Workflow API", version="0.4.0", lifespan=_api_lifespan)
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


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/camera/record/status")
def get_camera_record_status() -> Dict[str, Any]:
    from workflow.camera_executor import recording_camera_status

    return recording_camera_status()


@app.get("/api/hardware/status")
def get_hardware_status() -> Dict[str, Any]:
    owners = current_hardware_owners()
    safety = hardware_safety_status()
    return {
        "busy": bool(owners) or not bool(safety["safe"]),
        "safe": bool(safety["safe"]),
        "safety_latch": safety,
        "owners": owners,
        "owner": owners[0] if owners else {},
    }


@app.post("/api/camera/record/start", status_code=202)
def start_camera_record(req: CameraRecordStartRequest) -> Dict[str, Any]:
    try:
        return start_camera_recording(req)
    except CameraRecordServiceError as exc:
        raise _api_error(
            exc.status_code,
            exc.error_code,
            exc.message,
            log_detail=exc.log_detail,
            exc=exc.cause if exc.log_exception else None,
        ) from exc


@app.post("/api/camera/record/stop", status_code=202)
def stop_camera_record(_req: CameraRecordStopRequest | None = None) -> Dict[str, Any]:
    try:
        return stop_camera_recording()
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
    acquire_hardware_operation("stage_reciprocation", operation_id)
    try:
        result = stage_reciprocation_controller.start(cfg)
        confirm_hardware_operation("stage_reciprocation", operation_id)
        return result
    except HardwareGuardError:
        release_hardware_operation("stage_reciprocation", operation_id)
        raise
    except Exception as exc:
        release_hardware_operation("stage_reciprocation", operation_id)
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
        if result.get("status") in STAGE_TERMINAL_STATUSES:
            release_hardware_operation("stage_reciprocation", "stage_reciprocation")
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
    req = normalize_execute_task_request(req)
    try:
        return submit_task_request(req, access_logger=access_logger)
    except TaskRuntimeError as exc:
        raise _api_error(
            exc.status_code,
            exc.error_code,
            exc.message,
            log_detail=exc.log_detail,
        ) from exc


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> Dict[str, Any]:
    return cancel_task_request(task_id)


@app.get("/api/tasks/{task_id}/status")
def get_task_status(task_id: str) -> Dict[str, Any]:
    record = read_task_record(task_id)
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
    record = read_task_record(task_id)
    return build_task_result_response(record, TASK_ACTIVE_STATUSES, json_reader=read_json_with_retry)


@app.get("/api/tasks/{task_id}/wells/{well_name}/images")
def list_well_images(
    task_id: str,
    well_name: str,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int | None, Query(ge=1, le=1000)] = None,
) -> Dict[str, Any]:
    record = read_task_record(task_id)
    effective_limit = int(page_size if page_size is not None else limit)
    effective_offset = int((page - 1) * effective_limit if page is not None else offset)
    return build_well_images_response(record, well_name, limit=effective_limit, offset=effective_offset)


@app.get("/api/tasks/{task_id}/wells/{well_name}/images/{filename}")
def download_well_image(task_id: str, well_name: str, filename: str):
    record = read_task_record(task_id)
    file_path, media_type = resolve_well_image_file(record, well_name, filename)
    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=file_path.name,
    )


