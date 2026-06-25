"""
硬件互斥层，用于保证同一时间只有一个任务在使用硬件，避免硬件冲突。
记录当前硬件占用者
申请硬件占用
释放硬件占用
查询当前硬件占用状态
自动同步已结束的占用状态
允许特定并发策略
测试辅助
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict

from workflow.task_store import TASK_TERMINAL_STATUSES, read_task_record_unlocked, utc_now


HARDWARE_OWNER_SYNC_GRACE_SEC = 5.0
STAGE_TERMINAL_STATUSES = {"stopped", "failed"}

_HARDWARE_OPERATION_LOCK = threading.RLock()
_HARDWARE_OWNER: Dict[str, Any] | None = None
_CAMERA_RECORD_OWNER: Dict[str, Any] | None = None

logger = logging.getLogger(__name__)


class HardwareGuardError(RuntimeError):
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


def _hardware_busy_detail(owner: Dict[str, Any]) -> str:
    kind = owner.get("kind") or "unknown"
    operation_id = owner.get("operation_id") or "<unknown>"
    started_at = owner.get("started_at") or "<unknown>"
    return f"硬件正在被占用: kind={kind}, operation_id={operation_id}, started_at={started_at}"


def _public_hardware_owner(owner: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": owner.get("kind"),
        "operation_id": owner.get("operation_id"),
        "started_at": owner.get("started_at"),
    }


def _sync_hardware_owner_unlocked() -> None:
    global _HARDWARE_OWNER, _CAMERA_RECORD_OWNER

    owner = _HARDWARE_OWNER
    if owner and time.monotonic() >= float(owner.get("sync_after_monotonic") or 0.0):
        kind = owner.get("kind")
        operation_id = str(owner.get("operation_id") or "")
        try:
            if kind == "task":
                record = read_task_record_unlocked(operation_id)
                if record.get("status") in TASK_TERMINAL_STATUSES:
                    _HARDWARE_OWNER = None
            elif kind == "stage_reciprocation":
                from workflow.stage_reciprocation import stage_reciprocation_controller

                status = stage_reciprocation_controller.status()
                if status.get("status") in STAGE_TERMINAL_STATUSES:
                    _HARDWARE_OWNER = None
        except Exception:
            logger.debug("failed to sync hardware owner: %s", owner, exc_info=True)

    camera_owner = _CAMERA_RECORD_OWNER
    if camera_owner and time.monotonic() >= float(camera_owner.get("sync_after_monotonic") or 0.0):
        try:
            from workflow.camera_executor import recording_camera_status

            status = recording_camera_status()
            if not status.get("recording") and not status.get("background"):
                _CAMERA_RECORD_OWNER = None
        except Exception:
            logger.debug("failed to sync camera record owner: %s", camera_owner, exc_info=True)


def acquire_hardware_operation(kind: str, operation_id: str) -> Dict[str, Any]:
    global _HARDWARE_OWNER, _CAMERA_RECORD_OWNER

    with _HARDWARE_OPERATION_LOCK:
        _sync_hardware_owner_unlocked()
        if kind == "camera_record":
            if _HARDWARE_OWNER is not None:
                raise HardwareGuardError(
                    409,
                    "HARDWARE_BUSY",
                    "硬件正在执行其他任务，请稍后重试",
                    log_detail=_hardware_busy_detail(_HARDWARE_OWNER),
                )
            if _CAMERA_RECORD_OWNER is not None:
                raise HardwareGuardError(
                    409,
                    "CAMERA_RECORD_BUSY",
                    "相机录像已在进行中，请先停止当前录像",
                    log_detail=_hardware_busy_detail(_CAMERA_RECORD_OWNER),
                )
            _CAMERA_RECORD_OWNER = {
                "kind": kind,
                "operation_id": str(operation_id),
                "started_at": utc_now(),
                "sync_after_monotonic": time.monotonic() + HARDWARE_OWNER_SYNC_GRACE_SEC,
            }
            return _public_hardware_owner(_CAMERA_RECORD_OWNER)

        if _HARDWARE_OWNER is not None:
            raise HardwareGuardError(
                409,
                "HARDWARE_BUSY",
                "硬件正在执行其他任务，请稍后重试",
                log_detail=_hardware_busy_detail(_HARDWARE_OWNER),
            )
        _HARDWARE_OWNER = {
            "kind": kind,
            "operation_id": str(operation_id),
            "started_at": utc_now(),
            "sync_after_monotonic": time.monotonic() + HARDWARE_OWNER_SYNC_GRACE_SEC,
        }
        return _public_hardware_owner(_HARDWARE_OWNER)


def release_hardware_operation(kind: str, operation_id: str) -> None:
    global _HARDWARE_OWNER, _CAMERA_RECORD_OWNER

    with _HARDWARE_OPERATION_LOCK:
        if kind == "camera_record":
            if (
                _CAMERA_RECORD_OWNER is not None
                and str(_CAMERA_RECORD_OWNER.get("operation_id") or "") == str(operation_id)
            ):
                _CAMERA_RECORD_OWNER = None
            return

        if (
            _HARDWARE_OWNER is not None
            and _HARDWARE_OWNER.get("kind") == kind
            and str(_HARDWARE_OWNER.get("operation_id") or "") == str(operation_id)
        ):
            _HARDWARE_OWNER = None


def current_hardware_owner() -> Dict[str, Any] | None:
    with _HARDWARE_OPERATION_LOCK:
        _sync_hardware_owner_unlocked()
        return None if _HARDWARE_OWNER is None else _public_hardware_owner(_HARDWARE_OWNER)


def current_hardware_owners() -> list[Dict[str, Any]]:
    with _HARDWARE_OPERATION_LOCK:
        _sync_hardware_owner_unlocked()
        owners = []
        if _HARDWARE_OWNER is not None:
            owners.append(_public_hardware_owner(_HARDWARE_OWNER))
        if _CAMERA_RECORD_OWNER is not None:
            owners.append(_public_hardware_owner(_CAMERA_RECORD_OWNER))
        return owners


def reset_hardware_owners() -> None:
    global _HARDWARE_OWNER, _CAMERA_RECORD_OWNER

    with _HARDWARE_OPERATION_LOCK:
        _HARDWARE_OWNER = None
        _CAMERA_RECORD_OWNER = None
