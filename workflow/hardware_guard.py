"""维护任务、录像和往复运动的硬件互斥占用状态，避免并发操作冲突。"""
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


def _sync_task_and_stage_owner_unlocked() -> None:
    global _HARDWARE_OWNER

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


def _sync_camera_record_owner() -> None:
    """在硬件锁外核对录像占用，避免 status/SDK 阻塞把 acquire 和其它 API 一起堵住。"""
    global _CAMERA_RECORD_OWNER

    with _HARDWARE_OPERATION_LOCK:
        camera_owner = _CAMERA_RECORD_OWNER
        if not camera_owner or time.monotonic() < float(camera_owner.get("sync_after_monotonic") or 0.0):
            return

    try:
        from workflow.camera_executor import recording_camera_is_busy

        busy = recording_camera_is_busy()
    except Exception:
        logger.debug("failed to sync camera record owner: %s", camera_owner, exc_info=True)
        return

    if busy:
        return

    with _HARDWARE_OPERATION_LOCK:
        if _CAMERA_RECORD_OWNER is camera_owner:
            _CAMERA_RECORD_OWNER = None


def _camera_record_transition_snapshot() -> tuple[Dict[str, Any] | None, str | None]:
    """Read the local supervisor state without holding the hardware guard lock."""

    with _HARDWARE_OPERATION_LOCK:
        camera_owner = _CAMERA_RECORD_OWNER
    if camera_owner is None:
        return None, None
    try:
        from workflow.camera_executor import recording_camera_status

        state = str(recording_camera_status().get("state") or "")
    except Exception:
        logger.debug("failed to read camera transition state: %s", camera_owner, exc_info=True)
        state = None
    return camera_owner, state


def acquire_hardware_operation(kind: str, operation_id: str) -> Dict[str, Any]:
    global _HARDWARE_OWNER, _CAMERA_RECORD_OWNER

    _sync_camera_record_owner()
    transition_owner, camera_state = _camera_record_transition_snapshot()
    with _HARDWARE_OPERATION_LOCK:
        _sync_task_and_stage_owner_unlocked()
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

        current_camera_owner = _CAMERA_RECORD_OWNER
        if current_camera_owner is not None:
            owner_changed = current_camera_owner is not transition_owner
            transition = str(
                current_camera_owner.get("transition")
                or ("ownership-changed" if owner_changed else camera_state)
                or "unknown"
            )
            if owner_changed or transition != "recording":
                raise HardwareGuardError(
                    409,
                    "CAMERA_RECORD_TRANSITION",
                    "相机录像正在启动或停止，请等待录像状态稳定后重试",
                    log_detail=(
                        f"camera_record state={camera_state or 'unknown'}, "
                        f"transition={transition}; {_hardware_busy_detail(current_camera_owner)}"
                    ),
                )

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


def confirm_hardware_operation(kind: str, operation_id: str) -> None:
    """End the acquisition grace period after the runtime has reserved its state."""

    with _HARDWARE_OPERATION_LOCK:
        owner = _CAMERA_RECORD_OWNER if kind == "camera_record" else _HARDWARE_OWNER
        if (
            owner is not None
            and owner.get("kind") == kind
            and str(owner.get("operation_id") or "") == str(operation_id)
        ):
            owner["sync_after_monotonic"] = 0.0


def begin_camera_record_stop_transition() -> Dict[str, Any] | None:
    """Atomically block new hardware work before an active recording is stopped.

    A stable recording may be shared by a task.  Stopping the camera while that
    task is still running would invalidate its proxy halfway through a scan or
    autofocus cycle.  The transition marker and the ordinary hardware-owner
    check are protected by the same lock used by ``acquire_hardware_operation``.
    """

    _sync_camera_record_owner()
    with _HARDWARE_OPERATION_LOCK:
        _sync_task_and_stage_owner_unlocked()
        camera_owner = _CAMERA_RECORD_OWNER
        if camera_owner is None:
            return None
        if _HARDWARE_OWNER is not None:
            raise HardwareGuardError(
                409,
                "CAMERA_RECORD_STOP_BLOCKED",
                "硬件正在执行其他任务，请先等待或取消该任务后再停止录像",
                log_detail=_hardware_busy_detail(_HARDWARE_OWNER),
            )
        if camera_owner.get("transition"):
            raise HardwareGuardError(
                409,
                "CAMERA_RECORD_TRANSITION",
                "相机录像已经在执行启停转换，请等待状态稳定后重试",
                log_detail=_hardware_busy_detail(camera_owner),
            )
        camera_owner["transition"] = "stopping"
        camera_owner["sync_after_monotonic"] = 0.0
        return _public_hardware_owner(camera_owner)


def cancel_camera_record_stop_transition(operation_id: str) -> None:
    """Roll back a stop reservation when the supervisor rejected scheduling it."""

    with _HARDWARE_OPERATION_LOCK:
        camera_owner = _CAMERA_RECORD_OWNER
        if (
            camera_owner is not None
            and str(camera_owner.get("operation_id") or "") == str(operation_id)
            and camera_owner.get("transition") == "stopping"
        ):
            camera_owner.pop("transition", None)


def current_hardware_owner() -> Dict[str, Any] | None:
    _sync_camera_record_owner()
    with _HARDWARE_OPERATION_LOCK:
        _sync_task_and_stage_owner_unlocked()
        return None if _HARDWARE_OWNER is None else _public_hardware_owner(_HARDWARE_OWNER)


def current_hardware_owners() -> list[Dict[str, Any]]:
    _sync_camera_record_owner()
    with _HARDWARE_OPERATION_LOCK:
        _sync_task_and_stage_owner_unlocked()
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
