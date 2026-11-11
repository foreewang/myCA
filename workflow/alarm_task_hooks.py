"""Observation-only hooks and reconciliation for task/vision/storage alarms.

Task JSON and alarm SQLite are deliberately separate stores. Reconciliation
repairs persisted terminal facts after a failed alarm write, but cannot recover
transient faults which were never persisted in either store.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from functools import wraps
from pathlib import Path
from typing import Any

from workflow.alarm_service import report_alarm, recover_alarm
from workflow.task_control import TaskCanceled

logger = logging.getLogger(__name__)
_reconcile_lock = threading.Lock()
_reconciled: dict[str, tuple[int, int]] = {}


def reset_task_alarm_reconciliation() -> None:
    with _reconcile_lock:
        _reconciled.clear()


def storage_resource(path: str | Path) -> str:
    """Stable per-file deduplication without exposing local filesystem paths."""
    return hashlib.sha256(str(Path(path).absolute()).encode("utf-8")).hexdigest()[:24]


def execution_id_for_record(record: dict[str, Any]) -> str:
    explicit = record.get("execution_id")
    if explicit:
        return str(explicit)
    # Old records have no run UUID; immutable creation time distinguishes the
    # terminal records available during migration without rewriting task JSON.
    identity = json.dumps([record.get("task_id"), record.get("created_at")], ensure_ascii=True)
    return "legacy-" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def observe_task_record(record: dict[str, Any]) -> bool:
    status = record.get("status")
    code = {"failed": "TASK_FAILED", "interrupted": "TASK_INTERRUPTED", "canceled": "TASK_CANCELED"}.get(status)
    if code is None:
        return True
    # An ordinary admission conflict is not an operational production alarm.
    if status == "failed" and record.get("error_code") in {"HARDWARE_BUSY", "TASK_QUEUE_FULL"}:
        return True
    execution_id = execution_id_for_record(record)
    return report_alarm(
        code, component="task", resource=execution_id,
        task_id=str(record.get("task_id") or ""), execution_id=execution_id,
        fact=True, occurrence_key=f"{execution_id}:{status}",
        occurred_at=record.get("finished_at") or record.get("interrupted_at"),
    ) is not None


def reconcile_task_alarms() -> dict[str, int]:
    """Read changed task files once, retrying facts until alarm persistence succeeds."""
    from workflow.task_store import task_index_dir

    stats = {"scanned": 0, "reconciled": 0, "errors": 0}
    with _reconcile_lock:
        directory = task_index_dir()
        if not directory.exists():
            return stats
        current: set[str] = set()
        for path in directory.glob("*.json"):
            key = str(path.absolute())
            current.add(key)
            try:
                stamp = path.stat()
                signature = (stamp.st_mtime_ns, stamp.st_size)
                if _reconciled.get(key) == signature:
                    continue
                stats["scanned"] += 1
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("task record must be an object")
                recover_alarm("TASK_RECORD_READ_FAILED", component="task_store", resource=storage_resource(path))
                if observe_task_record(record):
                    _reconciled[key] = signature
                    stats["reconciled"] += 1
            except Exception:
                stats["errors"] += 1
                report_alarm("TASK_RECORD_READ_FAILED", component="task_store", resource=storage_resource(path))
                logger.exception("task alarm reconciliation could not read record resource=%s", storage_resource(path))
        for key in _reconciled.keys() - current:
            del _reconciled[key]
    return stats


def vision_resource(entrypoint: str | None) -> str:
    if not entrypoint or "instance_pipeline:" in entrypoint:
        return "model"
    if "detect_pipeline:" in entrypoint:
        return "rule"
    return "custom"


def report_vision_failure(exc: Exception, *, resource: str = "model") -> None:
    names = {cls.__name__ for cls in type(exc).__mro__}
    if "VisionInputQualityError" in names:
        code = "VISION_INPUT_QUALITY_FAILED"
    elif "VisionModelError" in names:
        code = "VISION_MODEL_FAILED"
    else:
        code = "VISION_INFERENCE_FAILED"
    report_alarm(code, component="vision", resource=resource)


def observe_vision_result(raw: Any, *, resource: str = "model") -> None:
    """Only a successful real inference establishes detector recovery."""
    for code in ("VISION_MODEL_FAILED", "VISION_INFERENCE_FAILED", "VISION_INPUT_QUALITY_FAILED"):
        recover_alarm(code, component="vision", resource=resource)
    if not isinstance(raw, dict):
        return
    runtime = raw.get("runtime")
    texture = raw.get("texture_processing")
    runtime = runtime if isinstance(runtime, dict) else {}
    texture = texture if isinstance(texture, dict) else {}
    stage_backends = texture.get("stage_backends")
    stage_backends = stage_backends if isinstance(stage_backends, dict) else {}
    fallback = runtime.get("fallback_reasons") or texture.get("fallback_reason")
    # The frozen CPU-only algorithm is an intentional backend, not GPU failure.
    if fallback and fallback != "previous_single_image_algorithm_uses_opencv_cpu":
        report_alarm("VISION_GPU_DEGRADED", component="vision", resource=resource)
    elif ("CUDAExecutionProvider" in str(runtime.get("backend", ""))
          or "cuda" in stage_backends.values()
          or texture.get("backend") == "cuda"):
        recover_alarm("VISION_GPU_DEGRADED", component="vision", resource=resource)


def configuration_boundary(resource: str, *, should_observe=None):
    """Report a failed execution preflight, leaving the original exception intact."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if should_observe is not None and not should_observe(*args, **kwargs):
                return fn(*args, **kwargs)
            try:
                result = fn(*args, **kwargs)
            except TaskCanceled:
                raise
            except Exception as exc:
                names = {cls.__name__ for cls in type(exc).__mro__}
                if "VisionModelError" in names:
                    report_vision_failure(exc)
                else:
                    report_alarm("CONFIG_EXECUTION_BLOCKED", component="configuration", resource=resource)
                exc._colony_alarm_reported = True
                raise
            recover_alarm("CONFIG_EXECUTION_BLOCKED", component="configuration", resource=resource)
            return result
        return wrapped
    return decorate
