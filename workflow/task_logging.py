"""Task identity scoped to an execution thread or async context."""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from inspect import signature
import logging
from time import perf_counter


current_task_id: ContextVar[str] = ContextVar("current_task_id", default="-")
current_request_id: ContextVar[str] = ContextVar("current_request_id", default="-")
current_stage: ContextVar[tuple | None] = ContextVar("current_stage", default=None)


def with_task_logging(argument: str, *, lifecycle: bool = False):
    """Bind a task ID for the call and restore it even when execution fails."""
    def decorate(function):
        call_signature = signature(function)

        @wraps(function)
        def wrapped(*args, **kwargs):
            arguments = call_signature.bind(*args, **kwargs).arguments
            value = arguments[argument]
            request_id = arguments.get("request_id") or getattr(value, "request_id", None)
            value = getattr(value, "task", value)
            if isinstance(value, dict):
                value = value.get("task", value).get("task_id")
            owns_lifecycle = lifecycle and current_task_id.get() == "-"
            token = current_task_id.set(str(value or "-"))
            request_token = current_request_id.set(request_id or current_request_id.get())
            stage_token = current_stage.set(None)
            logger = logging.getLogger("workflow.run_task")
            started = perf_counter()
            try:
                if owns_lifecycle:
                    logger.info("event=task_started")
                result = function(*args, **kwargs)
                if owns_lifecycle:
                    logger.info("event=task_completed elapsed_ms=%.1f", (perf_counter() - started) * 1000)
                return result
            except Exception as exc:
                if owns_lifecycle:
                    from workflow.task_control import TaskCanceled
                    if isinstance(exc, TaskCanceled):
                        logger.info("event=task_canceled elapsed_ms=%.1f", (perf_counter() - started) * 1000)
                    else:
                        logger.exception("event=task_failed error_code=TASK_EXECUTION_FAILED elapsed_ms=%.1f",
                                         (perf_counter() - started) * 1000)
                raise
            finally:
                current_task_id.reset(token)
                current_request_id.reset(request_token)
                current_stage.reset(stage_token)

        return wrapped

    return decorate
