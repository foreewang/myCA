"""提供任务取消检查和安全检查点中断异常。"""
from __future__ import annotations

from typing import Any, Callable, Mapping
import logging

from workflow.task_logging import current_stage, current_task_id


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


def _coerce_progress(value: Any) -> int:
    try:
        progress = int(round(float(value)))
    except Exception:
        progress = 0
    return max(0, min(100, progress))


def report_progress(
    params: Mapping[str, Any] | None,
    stage: str,
    progress: int | float,
    well: str | None = None,
    message: str | None = None,
) -> None:
    if not params:
        return
    stage_key = (str(stage), well or params.get("well_name"))
    if current_task_id.get() != "-" and current_stage.get() != stage_key:
        current_stage.set(stage_key)
        logging.getLogger("workflow.run_task").info(
            "event=task_stage stage=%s well=%s", stage_key[0], stage_key[1] or "-"
        )
    callback = params.get("_progress_callback")
    if not callable(callback):
        return

    local_progress = _coerce_progress(progress)
    try:
        base = float(params.get("_progress_base", 0.0) or 0.0)
        span = float(params.get("_progress_span", 100.0) or 100.0)
    except Exception:
        base = 0.0
        span = 100.0
    task_progress = _coerce_progress(base + span * (local_progress / 100.0))
    current_well = well or params.get("well_name")
    try:
        callback(str(stage), task_progress, str(current_well) if current_well else None, str(message or stage))
    except Exception:
        return
