from __future__ import annotations

from typing import Any, Callable, Mapping


class TaskCanceled(RuntimeError):
    """Raised when a workflow task is asked to stop at a safe checkpoint."""


def is_cancel_requested(params: Mapping[str, Any] | None) -> bool:
    if not params:
        return False
    checker = params.get("_cancel_check")
    if checker is None:
        return False
    if not callable(checker):
        return bool(checker)
    return bool(checker())


def raise_if_cancel_requested(params: Mapping[str, Any] | None, stage: str = "") -> None:
    if not is_cancel_requested(params):
        return
    suffix = f": {stage}" if stage else ""
    raise TaskCanceled(f"任务已请求取消{suffix}")
