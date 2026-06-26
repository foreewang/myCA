"""
API错误处理层，负责：
统一错误响应结构：error_code + message
_api_error 对应的新实现：api_error
HTTP 异常兜底消息转换
Pydantic 参数校验错误处理
TaskStoreError / HardwareGuardError / PathGuardError 统一转 API 响应
未捕获异常统一返回 500，并把详细堆栈写入本地日志
API 日志文件配置：C:/colony_system/logs/api_server.log
register_api_error_handlers(app) 统一注册 FastAPI 异常处理器
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from typing import Any, Iterable

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from workflow.hardware_guard import HardwareGuardError, logger as hardware_guard_logger
from workflow.path_guard import PROJECT_ROOT, PathGuardError
from workflow.task_store import TaskStoreError, logger as task_store_logger

LOG_DIR = PROJECT_ROOT / "logs"
API_LOG_PATH = LOG_DIR / "api_server.log"
API_LOG_MAX_BYTES = 10 * 1024 * 1024
API_LOG_BACKUP_COUNT = 5
API_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("uvicorn.error")


def configure_api_file_logging(extra_loggers: Iterable[logging.Logger] | None = None) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = str(API_LOG_PATH.resolve(strict=False))
    formatter = logging.Formatter(API_LOG_FORMAT)

    target_loggers = [logger, access_logger, task_store_logger, hardware_guard_logger]
    if extra_loggers:
        for extra_logger in extra_loggers:
            if not any(existing is extra_logger for existing in target_loggers):
                target_loggers.append(extra_logger)

    for target_logger in target_loggers:
        if any(getattr(handler, "_colony_api_log_path", None) == log_path for handler in target_logger.handlers):
            continue
        handler = RotatingFileHandler(
            log_path,
            maxBytes=API_LOG_MAX_BYTES,
            backupCount=API_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(formatter)
        setattr(handler, "_colony_api_log_path", log_path)
        target_logger.addHandler(handler)
        if target_logger.getEffectiveLevel() > logging.INFO:
            target_logger.setLevel(logging.INFO)


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
        logger.exception("%s: %s", error_code, log_detail or message)
    elif log_detail is not None:
        logger.warning("%s: %s", error_code, log_detail)
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
    logger.warning("%s: path=%s detail=%r", error_code, request.url.path, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": error_detail(error_code, generic_http_message(exc.status_code))},
        headers=exc.headers,
    )


async def request_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.warning(
        "REQUEST_VALIDATION_FAILED: path=%s errors=%s body=%r",
        request.url.path,
        exc.errors(),
        exc.body,
    )
    return JSONResponse(
        status_code=422,
        content={"detail": error_detail("REQUEST_VALIDATION_FAILED", "请求参数不合法")},
    )


async def task_store_exception_handler(_request: Request, exc: TaskStoreError) -> JSONResponse:
    if exc.cause is not None:
        logger.exception("%s: %s", exc.error_code, exc.log_detail or exc.message)
    elif exc.log_detail is not None:
        logger.warning("%s: %s", exc.error_code, exc.log_detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": error_detail(exc.error_code, exc.message)},
    )


async def hardware_guard_exception_handler(_request: Request, exc: HardwareGuardError) -> JSONResponse:
    if exc.log_detail is not None:
        logger.warning("%s: %s", exc.error_code, exc.log_detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": error_detail(exc.error_code, exc.message)},
    )


async def path_guard_exception_handler(_request: Request, exc: PathGuardError) -> JSONResponse:
    if exc.log_detail is not None:
        logger.warning("%s: %s", exc.error_code, exc.log_detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": error_detail(exc.error_code, exc.message)},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("INTERNAL_SERVER_ERROR: path=%s", request.url.path)
    return JSONResponse(
        status_code=500,
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
    app.add_exception_handler(Exception, unhandled_exception_handler)
