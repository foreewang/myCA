"""提供 Colony Workflow 的 FastAPI 服务入口并挂载任务、硬件、录像和产物查询接口。"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from uuid import uuid4

from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator, Callable, Dict

from workflow.task_logging import current_request_id
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fastapi import FastAPI, Query, Request
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
    STAGE_TERMINAL_STATUSES,
    acquire_hardware_operation,
    current_hardware_owners,
    release_hardware_operation,
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
    start_task_runtime_manager,
    stop_task_runtime_manager,
    submit_task_request,
)
from workflow.task_artifacts import (
    build_task_result_response,
    build_well_images_response,
    resolve_well_image_file,
    resolve_pickable_result_file,
)

logger = logging.getLogger(__name__)
camera_supervisor_logger = logging.getLogger("workflow.camera_process_supervisor")


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


@asynccontextmanager
async def _api_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _configure_api_file_logging()
    single_worker_lock: SingleInstanceLock | None = None
    camera_supervisor_initialized = False
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
        logger.info("api single-worker lock acquired: %s", single_worker_lock.path)
        recover_interrupted_task_records()
        # Validate all camera-process timeout environment variables during
        # lifespan startup.  This creates only the local supervisor; the MVS
        # child process remains lazy until the first camera operation.
        from workflow.camera_process_supervisor import initialize_camera_process_supervisor

        initialize_camera_process_supervisor()
        camera_supervisor_initialized = True
        start_task_runtime_manager()
        yield
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        cleanup_cancellation: asyncio.CancelledError | None = None
        try:
            try:
                stopped, cancellation, error = await _await_thread_cleanup(stop_task_runtime_manager)
                if cancellation is not None:
                    cleanup_cancellation = cancellation
                if error is not None:
                    if isinstance(error, asyncio.CancelledError):
                        cleanup_cancellation = cleanup_cancellation or error
                    else:
                        cleanup_error = error
                        _log_cleanup_error("failed to stop task runtime during API shutdown", error)
                elif not stopped:
                    logger.warning("task runtime worker did not stop within timeout")
            except asyncio.CancelledError as exc:
                cleanup_cancellation = cleanup_cancellation or exc
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
                _log_cleanup_error("failed to schedule task runtime shutdown", exc)

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
            if single_worker_lock is not None:
                try:
                    single_worker_lock.release()
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
                    _log_cleanup_error("failed to release API single-worker lock", exc)

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


class RequestLoggingMiddleware:
    """Keep correlation until the response body and background work finish."""
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        token = current_request_id.set(request_id)
        status = 500
        outcome = "completed"

        async def send_response(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_response)
        except BaseException:
            outcome = "failed"
            raise
        finally:
            route = scope.get("route")
            path = getattr(route, "path", "<unmatched>")
            # Sync endpoints run in a thread pool: ContextVar changes there do
            # not flow back here. Request state and resolved path params do.
            task_id = (scope.get("state", {}).get("log_task_id")
                       or scope.get("path_params", {}).get("task_id") or "-")
            try:
                access_logger.info(
                    "event=request_completed method=%s path=%s status=%s outcome=%s elapsed_ms=%.1f",
                    scope["method"], path, status, outcome, (time.perf_counter() - started) * 1000.0,
                    extra={"http_route": path, "task_id": str(task_id)},
                )
            finally:
                current_request_id.reset(token)


app.add_middleware(RequestLoggingMiddleware)


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
    return {
        "busy": bool(owners),
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
        return stage_reciprocation_controller.start(cfg)
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
def execute_task(req: ExecuteTaskRequest, request: Request) -> Dict[str, Any]:
    req = normalize_execute_task_request(req)
    try:
        result = submit_task_request(req, access_logger=access_logger)
        request.state.log_task_id = result["task_id"]
        return result
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


@app.get("/api/tasks/{task_id}/wells/{well_name}/pickable-result")
def get_pickable_result(task_id: str, well_name: str, download: bool = False):
    path = resolve_pickable_result_file(read_task_record(task_id), well_name)
    return FileResponse(path=path, media_type="application/json",
                        filename=path.name if download else None)


@app.get("/api/tasks/{task_id}/wells/{well_name}/images/{filename}")
def download_well_image(task_id: str, well_name: str, filename: str):
    record = read_task_record(task_id)
    file_path, media_type = resolve_well_image_file(record, well_name, filename)
    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=file_path.name,
    )


