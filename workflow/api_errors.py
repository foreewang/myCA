"""统一配置 API 日志、错误响应结构和 FastAPI 异常处理器。"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from workflow.camera_record_service import CameraRecordServiceError
from workflow.hardware_guard import HardwareGuardError
from workflow.log_sanitizer import log_redaction_enabled, sanitize_log_detail
from workflow.logging_config import LOG_DIR, configure_logging
from workflow.path_guard import PathGuardError
from workflow.task_artifacts import TaskArtifactError
from workflow.task_runtime import TaskRuntimeError
from workflow.task_store import TaskStoreError

API_LOG_PATH = LOG_DIR / "api_server.log"
ACCESS_LOG_PATH = LOG_DIR / "api_access.log"
TASK_LOG_PATH = LOG_DIR / "task.log"

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("workflow.access")
workflow_logger = logging.getLogger("workflow")


def _log_warning(error_code: str, detail: Any) -> None:
    logger.warning("%s: %s", error_code, sanitize_log_detail(detail))


def _log_exception(error_code: str, detail: Any, exc: BaseException | None = None) -> None:
    sanitized = sanitize_log_detail(detail)
    if exc is not None and log_redaction_enabled():
        logger.error("%s: %s exc_type=%s", error_code, sanitized, type(exc).__name__)
        return
    if exc is not None:
        logger.error("%s: %s", error_code, sanitized, exc_info=(type(exc), exc, exc.__traceback__))
    else:
        logger.exception("%s: %s", error_code, sanitized)


def configure_api_file_logging(extra_loggers: Iterable[logging.Logger] | None = None) -> None:
    configure_logging(paths={"api": API_LOG_PATH, "access": ACCESS_LOG_PATH, "task": TASK_LOG_PATH},
                      extra_loggers=extra_loggers or ())


def error_detail(error_code: str, message: str) -> dict[str, str]:
    return {
        "error_code": error_code,
        "message": message,
    }


def api_error(
    status_code: int,
    error_code: str,
    message: str,
    *,
    log_detail: str | None = None,
    exc: BaseException | None = None,
) -> HTTPException:
    if exc is not None:
        _log_exception(error_code, log_detail or message, exc)
    elif log_detail is not None:
        _log_warning(error_code, log_detail)
    return HTTPException(status_code=status_code, detail=error_detail(error_code, message))


def generic_http_message(status_code: int) -> str:
    if status_code == 400:
        return "请求参数不合法"
    if status_code == 401:
        return "未认证或认证已失效"
    if status_code == 403:
        return "没有权限执行该操作"
    if status_code == 404:
        return "请求的资源不存在"
    if status_code == 409:
        return "请求与当前系统状态冲突"
    if status_code == 422:
        return "请求参数不合法"
    if status_code >= 500:
        return "服务内部错误，请查看本地日志或联系维护人员"
    return "请求处理失败"


def is_public_error_detail(detail: Any) -> bool:
    return (
        isinstance(detail, dict)
        and isinstance(detail.get("error_code"), str)
        and isinstance(detail.get("message"), str)
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if is_public_error_detail(exc.detail):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    error_code = f"HTTP_{exc.status_code}"
    _log_warning(error_code, f"path={request.url.path} detail={exc.detail!r}")
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": error_detail(error_code, generic_http_message(exc.status_code))},
        headers=exc.headers,
    )


async def request_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    _log_warning(
        "REQUEST_VALIDATION_FAILED",
        f"path={request.url.path} errors={exc.errors()} body={exc.body!r}",
    )
    return JSONResponse(
        status_code=422,
        content={"detail": error_detail("REQUEST_VALIDATION_FAILED", "请求参数不合法")},
    )


async def task_store_exception_handler(_request: Request, exc: TaskStoreError) -> JSONResponse:
    if exc.cause is not None:
        _log_exception(exc.error_code, exc.log_detail or exc.message, exc.cause)
    elif exc.log_detail is not None:
        _log_warning(exc.error_code, exc.log_detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": error_detail(exc.error_code, exc.message)},
    )


async def hardware_guard_exception_handler(_request: Request, exc: HardwareGuardError) -> JSONResponse:
    if exc.log_detail is not None:
        _log_warning(exc.error_code, exc.log_detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": error_detail(exc.error_code, exc.message)},
    )


async def path_guard_exception_handler(_request: Request, exc: PathGuardError) -> JSONResponse:
    if exc.log_detail is not None:
        _log_warning(exc.error_code, exc.log_detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": error_detail(exc.error_code, exc.message)},
    )


async def task_artifact_exception_handler(_request: Request, exc: TaskArtifactError) -> JSONResponse:
    if exc.cause is not None:
        _log_exception(exc.error_code, exc.log_detail or exc.message, exc.cause)
    elif exc.log_detail is not None:
        _log_warning(exc.error_code, exc.log_detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": error_detail(exc.error_code, exc.message)},
    )


async def camera_record_service_exception_handler(_request: Request, exc: CameraRecordServiceError) -> JSONResponse:
    if exc.cause is not None and exc.log_exception:
        _log_exception(exc.error_code, exc.log_detail or exc.message, exc.cause)
    elif exc.log_detail is not None:
        _log_warning(exc.error_code, exc.log_detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": error_detail(exc.error_code, exc.message)},
    )


async def task_runtime_exception_handler(_request: Request, exc: TaskRuntimeError) -> JSONResponse:
    if exc.log_detail is not None:
        _log_warning(exc.error_code, exc.log_detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": error_detail(exc.error_code, exc.message)},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "-")
    logger.error("event=request_failed error_code=INTERNAL_SERVER_ERROR",
                 exc_info=(type(exc), exc, exc.__traceback__), extra={"request_id": request_id})
    exc._colony_logged = True
    return JSONResponse(
        status_code=500,
        headers={"X-Request-ID": request_id},
        content={
            "detail": error_detail(
                "INTERNAL_SERVER_ERROR",
                "服务内部错误，请查看本地日志或联系维护人员",
            )
        },
    )


def register_api_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.add_exception_handler(TaskStoreError, task_store_exception_handler)
    app.add_exception_handler(HardwareGuardError, hardware_guard_exception_handler)
    app.add_exception_handler(PathGuardError, path_guard_exception_handler)
    app.add_exception_handler(TaskArtifactError, task_artifact_exception_handler)
    app.add_exception_handler(CameraRecordServiceError, camera_record_service_exception_handler)
    app.add_exception_handler(TaskRuntimeError, task_runtime_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
