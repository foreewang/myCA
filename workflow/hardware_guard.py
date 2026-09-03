"""维护任务、录像和往复运动的硬件互斥占用状态，避免并发操作冲突。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict

from workflow.file_io import atomic_write_json, read_json_with_retry
from workflow.path_guard import DATA_ROOT, PROJECT_ROOT
from workflow.task_store import TASK_TERMINAL_STATUSES, read_task_record_unlocked, utc_now


HARDWARE_OWNER_SYNC_GRACE_SEC = 5.0
STAGE_TERMINAL_STATUSES = {"stopped", "failed"}
HARDWARE_SAFETY_LATCH_VERSION = 1
PRODUCTION_HARDWARE_SAFETY_LATCH_PATH = (
    DATA_ROOT / "hardware_safety_latch.json"
).resolve(strict=False)
# Test suites replace this default with an isolated temporary path. Production
# recovery code must use ``PRODUCTION_HARDWARE_SAFETY_LATCH_PATH`` directly and
# never accept a caller-selected latch location.
DEFAULT_HARDWARE_SAFETY_LATCH_PATH = PRODUCTION_HARDWARE_SAFETY_LATCH_PATH

_HARDWARE_OPERATION_LOCK = threading.RLock()
_HARDWARE_SAFETY_PERSIST_LOCK = threading.RLock()
_HARDWARE_OWNER: Dict[str, Any] | None = None
_CAMERA_RECORD_OWNER: Dict[str, Any] | None = None
_HARDWARE_SAFETY_LATCH: Dict[str, Any] | None = None
_HARDWARE_SAFETY_LATCH_PATH: Path | None = None
_HARDWARE_SAFETY_LATCH_PERSISTED = False
_HARDWARE_SAFETY_GENERATION_ID: str | None = None
_HARDWARE_SAFETY_GENERATION_ACTIVE = False
_HARDWARE_ADMISSION_CLOSED = True

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


def _configured_hardware_safety_latch_path(path: str | Path | None = None) -> Path:
    raw = path
    if raw is None or not str(raw).strip():
        candidate = DEFAULT_HARDWARE_SAFETY_LATCH_PATH
    else:
        candidate = Path(str(raw).strip())
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
    return candidate.resolve(strict=False)


def _persisted_latch_payload(latch: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "version": HARDWARE_SAFETY_LATCH_VERSION,
        "record_type": "unsafe_latch",
        "safe": False,
        "latch_id": str(latch["latch_id"]),
        "error_code": str(latch["error_code"]),
        "message": str(latch["message"]),
        "latched_at": str(latch["latched_at"]),
    }


def _validate_persisted_latch(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("safety latch payload must be an object")
    if payload.get("version") != HARDWARE_SAFETY_LATCH_VERSION:
        raise ValueError("unsupported safety latch version")
    if payload.get("record_type") not in (None, "unsafe_latch"):
        raise ValueError("safety latch record type is invalid")
    if payload.get("safe") is not False:
        raise ValueError("a persisted safety latch must be unsafe")
    normalized = {"safe": False}
    for field in ("latch_id", "error_code", "message", "latched_at"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"safety latch field is missing: {field}")
        normalized[field] = value.strip()
    return normalized


def _generation_marker_payload(generation_id: str) -> Dict[str, Any]:
    return {
        "version": HARDWARE_SAFETY_LATCH_VERSION,
        "record_type": "generation_active",
        "generation_id": str(generation_id),
        "pid": os.getpid(),
        "started_at": utc_now(),
    }


def _validate_generation_marker(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("generation marker payload must be an object")
    if payload.get("version") != HARDWARE_SAFETY_LATCH_VERSION:
        raise ValueError("unsupported generation marker version")
    if payload.get("record_type") != "generation_active":
        raise ValueError("not an active generation marker")
    generation_id = payload.get("generation_id")
    started_at = payload.get("started_at")
    pid = payload.get("pid")
    if not isinstance(generation_id, str) or not generation_id.strip():
        raise ValueError("generation marker id is missing")
    if not isinstance(started_at, str) or not started_at.strip():
        raise ValueError("generation marker timestamp is missing")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("generation marker pid is invalid")
    return {
        "version": HARDWARE_SAFETY_LATCH_VERSION,
        "record_type": "generation_active",
        "generation_id": generation_id.strip(),
        "pid": pid,
        "started_at": started_at.strip(),
    }


def _write_generation_marker_exclusive(path: Path, payload: Dict[str, Any]) -> None:
    """Create the write-ahead marker without overwriting a concurrent marker.

    A crash during this direct write can leave a partial file. That is safe:
    the next generation treats any unreadable marker as an unsafe condition.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(path), flags, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("generation marker write made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _persist_hardware_safety_latch(latch: Dict[str, Any]) -> bool:
    """Persist and read back the exact latch before treating it as durable."""

    global _HARDWARE_SAFETY_LATCH_PERSISTED, _HARDWARE_SAFETY_GENERATION_ACTIVE
    with _HARDWARE_OPERATION_LOCK:
        path = _HARDWARE_SAFETY_LATCH_PATH
    if path is None:
        logger.critical(
            "hardware safety latch is not durable because persistence was not initialized"
        )
        return False

    expected = _persisted_latch_payload(latch)
    try:
        with _HARDWARE_SAFETY_PERSIST_LOCK:
            atomic_write_json(path, expected)
            verified = _validate_persisted_latch(read_json_with_retry(path))
        if verified.get("latch_id") != expected["latch_id"]:
            raise RuntimeError("persisted safety latch identity does not match")
    except BaseException:
        with _HARDWARE_OPERATION_LOCK:
            if (
                _HARDWARE_SAFETY_LATCH is not None
                and _HARDWARE_SAFETY_LATCH.get("latch_id") == latch.get("latch_id")
            ):
                _HARDWARE_SAFETY_LATCH_PERSISTED = False
        logger.critical(
            "failed to persist hardware safety latch; process generation must remain fenced",
            exc_info=True,
        )
        return False

    with _HARDWARE_OPERATION_LOCK:
        if (
            _HARDWARE_SAFETY_LATCH is not None
            and _HARDWARE_SAFETY_LATCH.get("latch_id") == latch.get("latch_id")
            and _HARDWARE_SAFETY_LATCH_PATH == path
        ):
            _HARDWARE_SAFETY_LATCH_PERSISTED = True
            _HARDWARE_SAFETY_GENERATION_ACTIVE = False
            return True
    return False


def restore_persisted_hardware_safety_latch(
    path: str | Path | None = None,
) -> Dict[str, Any]:
    """Initialize persistence and fail closed if an unsafe marker is present.

    The API calls this only after it owns the cross-process generation lock and
    before any hardware runtime is started. A malformed or unreadable marker
    is deliberately treated as unsafe rather than silently discarded.
    """

    global _HARDWARE_SAFETY_LATCH, _HARDWARE_SAFETY_LATCH_PATH
    global _HARDWARE_SAFETY_LATCH_PERSISTED
    global _HARDWARE_SAFETY_GENERATION_ID, _HARDWARE_SAFETY_GENERATION_ACTIVE

    configured_path = _configured_hardware_safety_latch_path(path)
    with _HARDWARE_OPERATION_LOCK:
        if (
            _HARDWARE_SAFETY_LATCH_PATH is not None
            and _HARDWARE_SAFETY_LATCH_PATH != configured_path
        ):
            raise RuntimeError(
                "hardware safety latch persistence path cannot change within a process"
            )
        _HARDWARE_SAFETY_LATCH_PATH = configured_path
        existing_latch = (
            None if _HARDWARE_SAFETY_LATCH is None else dict(_HARDWARE_SAFETY_LATCH)
        )

    if existing_latch is not None:
        if not _persist_hardware_safety_latch(existing_latch):
            raise RuntimeError("failed to make existing hardware safety latch durable")
        return hardware_safety_status()

    if not configured_path.exists():
        generation_id = uuid.uuid4().hex
        marker = _generation_marker_payload(generation_id)
        try:
            with _HARDWARE_SAFETY_PERSIST_LOCK:
                _write_generation_marker_exclusive(configured_path, marker)
                verified_marker = _validate_generation_marker(
                    read_json_with_retry(configured_path)
                )
            if verified_marker["generation_id"] != generation_id:
                raise RuntimeError("generation marker identity does not match")
        except FileExistsError:
            # Never overwrite an unexpected marker. Inspect it below and fail
            # closed; compliant writers are serialized by the API lock.
            pass
        except BaseException as exc:
            raise RuntimeError(
                "failed to establish hardware safety generation marker before startup"
            ) from exc
        else:
            with _HARDWARE_OPERATION_LOCK:
                _HARDWARE_SAFETY_GENERATION_ID = generation_id
                _HARDWARE_SAFETY_GENERATION_ACTIVE = True
                _HARDWARE_SAFETY_LATCH_PERSISTED = True
            return hardware_safety_status()

    try:
        persisted_payload = read_json_with_retry(configured_path)
        try:
            restored = _validate_persisted_latch(persisted_payload)
        except (TypeError, ValueError):
            stale_generation = _validate_generation_marker(persisted_payload)
            restored = {
                "safe": False,
                "latch_id": f"unclean-{stale_generation['generation_id']}",
                "error_code": "HARDWARE_GENERATION_UNCLEAN_EXIT",
                "message": (
                    "检测到上一个 API 代际未完成安全关停；禁止硬件操作，需停机检查并按运维流程清除"
                ),
                "latched_at": stale_generation["started_at"],
            }
    except Exception as exc:
        restored = {
            "safe": False,
            "latch_id": f"invalid-{uuid.uuid4().hex}",
            "error_code": "HARDWARE_SAFETY_LATCH_FILE_INVALID",
            "message": (
                "硬件安全锁存文件无法验证；禁止硬件操作，需停机检查并按运维流程清除"
            ),
            "latched_at": utc_now(),
        }
        logger.critical(
            "hardware safety latch file is unreadable or invalid; failing closed: path=%s",
            configured_path,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    else:
        logger.critical(
            "restored persistent hardware safety latch: error_code=%s latched_at=%s",
            restored["error_code"],
            restored["latched_at"],
        )

    with _HARDWARE_OPERATION_LOCK:
        if _HARDWARE_SAFETY_LATCH is None:
            _HARDWARE_SAFETY_LATCH = restored
        _HARDWARE_SAFETY_LATCH_PERSISTED = False
        _HARDWARE_SAFETY_GENERATION_ID = None
        _HARDWARE_SAFETY_GENERATION_ACTIVE = False
        restored_latch = dict(_HARDWARE_SAFETY_LATCH)
    # Replace stale/invalid/legacy input with a validated unsafe record and
    # read it back. If the file vanished or storage is unavailable, startup
    # must hard-fail before any hardware runtime is initialized.
    if not _persist_hardware_safety_latch(restored_latch):
        raise RuntimeError("failed to establish a durable hardware safety latch")
    return hardware_safety_status()


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


def close_hardware_admission_for_shutdown() -> None:
    """Reject new hardware owners before runtime shutdown begins."""

    global _HARDWARE_ADMISSION_CLOSED
    with _HARDWARE_OPERATION_LOCK:
        _HARDWARE_ADMISSION_CLOSED = True


def open_hardware_admission_after_startup() -> None:
    """Open admission after the write-ahead marker and runtimes are ready."""

    global _HARDWARE_ADMISSION_CLOSED
    with _HARDWARE_OPERATION_LOCK:
        _HARDWARE_ADMISSION_CLOSED = False


def _sync_task_and_stage_owner() -> None:
    """Reconcile a stale owner without holding the hardware admission lock.

    Task records can encounter slow filesystem retries and stage ``status()``
    takes the controller lock. Neither external dependency may run while the
    hardware lock is held, otherwise API admission can deadlock against a
    stage start that validates the safety latch in the opposite order.
    """

    global _HARDWARE_OWNER

    with _HARDWARE_OPERATION_LOCK:
        owner = _HARDWARE_OWNER
        if owner is None:
            return
        sync_after = float(owner.get("sync_after_monotonic") or 0.0)
        if not bool(owner.get("sync_enabled", True)):
            return
        if time.monotonic() < sync_after:
            return
        kind = owner.get("kind")
        operation_id = str(owner.get("operation_id") or "")

    terminal = False
    try:
        if kind == "task":
            record = read_task_record_unlocked(operation_id)
            terminal = record.get("status") in TASK_TERMINAL_STATUSES
        elif kind == "stage_reciprocation":
            from workflow.stage_reciprocation import stage_reciprocation_controller

            status = stage_reciprocation_controller.status()
            terminal = status.get("status") in STAGE_TERMINAL_STATUSES
    except Exception:
        logger.debug("failed to sync hardware owner: %s", owner, exc_info=True)
        return

    if not terminal:
        return
    with _HARDWARE_OPERATION_LOCK:
        # Identity prevents a delayed external read from clearing a replacement
        # owner. The deadline comparison also protects a concurrently confirmed
        # async start, which mutates the existing owner in place.
        if (
            _HARDWARE_OWNER is owner
            and float(owner.get("sync_after_monotonic") or 0.0) == sync_after
        ):
            _HARDWARE_OWNER = None


def _sync_camera_record_owner() -> None:
    """在硬件锁外核对录像占用，避免 status/SDK 阻塞把 acquire 和其它 API 一起堵住。"""
    global _CAMERA_RECORD_OWNER

    with _HARDWARE_OPERATION_LOCK:
        camera_owner = _CAMERA_RECORD_OWNER
        if not camera_owner or not bool(camera_owner.get("sync_enabled", True)):
            return
        sync_after = float(camera_owner.get("sync_after_monotonic") or 0.0)
        if time.monotonic() < sync_after:
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
        if (
            _CAMERA_RECORD_OWNER is camera_owner
            and bool(camera_owner.get("sync_enabled", True))
            and float(camera_owner.get("sync_after_monotonic") or 0.0) == sync_after
        ):
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
    _sync_task_and_stage_owner()
    transition_owner, camera_state = _camera_record_transition_snapshot()
    with _HARDWARE_OPERATION_LOCK:
        if _HARDWARE_SAFETY_LATCH is not None:
            raise HardwareGuardError(
                503,
                "HARDWARE_SAFETY_LATCHED",
                "硬件停止状态未确认，禁止继续操作；请停机检查并按运维流程清除安全锁存",
                log_detail=str(_HARDWARE_SAFETY_LATCH.get("error_code") or "unknown"),
            )
        if _HARDWARE_ADMISSION_CLOSED:
            raise HardwareGuardError(
                503,
                "HARDWARE_ADMISSION_CLOSED",
                "硬件服务正在启动或关闭，暂不接受新的硬件操作",
            )
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
                "sync_enabled": False,
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
            "sync_enabled": False,
        }
        return _public_hardware_owner(_HARDWARE_OWNER)


def require_hardware_safe() -> None:
    """Fail when unsafe or when startup/shutdown has closed new admission."""

    with _HARDWARE_OPERATION_LOCK:
        latch = None if _HARDWARE_SAFETY_LATCH is None else dict(_HARDWARE_SAFETY_LATCH)
        admission_closed = _HARDWARE_ADMISSION_CLOSED
    if latch is not None:
        raise HardwareGuardError(
            503,
            "HARDWARE_SAFETY_LATCHED",
            "硬件停止状态未确认，禁止继续操作；请停机检查并按运维流程清除安全锁存",
            log_detail=str(latch.get("error_code") or "unknown"),
        )
    if admission_closed:
        raise HardwareGuardError(
            503,
            "HARDWARE_ADMISSION_CLOSED",
            "硬件服务正在启动或关闭，暂不接受新的硬件操作",
        )


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
            owner["sync_enabled"] = True


def begin_camera_record_stop_transition() -> Dict[str, Any] | None:
    """Atomically block new hardware work before an active recording is stopped.

    A stable recording may be shared by a task.  Stopping the camera while that
    task is still running would invalidate its proxy halfway through a scan or
    autofocus cycle.  The transition marker and the ordinary hardware-owner
    check are protected by the same lock used by ``acquire_hardware_operation``.
    """

    _sync_camera_record_owner()
    _sync_task_and_stage_owner()
    with _HARDWARE_OPERATION_LOCK:
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
    _sync_task_and_stage_owner()
    with _HARDWARE_OPERATION_LOCK:
        return None if _HARDWARE_OWNER is None else _public_hardware_owner(_HARDWARE_OWNER)


def current_hardware_owners() -> list[Dict[str, Any]]:
    _sync_camera_record_owner()
    _sync_task_and_stage_owner()
    with _HARDWARE_OPERATION_LOCK:
        owners = []
        if _HARDWARE_OWNER is not None:
            owners.append(_public_hardware_owner(_HARDWARE_OWNER))
        if _CAMERA_RECORD_OWNER is not None:
            owners.append(_public_hardware_owner(_CAMERA_RECORD_OWNER))
        return owners


def latch_hardware_unsafe(error_code: str, message: str) -> Dict[str, Any]:
    """Close hardware admission and persist the unsafe state across restarts."""

    global _HARDWARE_SAFETY_LATCH, _HARDWARE_SAFETY_LATCH_PERSISTED
    with _HARDWARE_OPERATION_LOCK:
        if _HARDWARE_SAFETY_LATCH is None:
            _HARDWARE_SAFETY_LATCH = {
                "safe": False,
                "latch_id": uuid.uuid4().hex,
                "error_code": str(error_code),
                "message": str(message),
                "latched_at": utc_now(),
            }
            _HARDWARE_SAFETY_LATCH_PERSISTED = False
            logger.critical(
                "hardware safety latch engaged: error_code=%s message=%s",
                error_code,
                message,
            )
        latched = dict(_HARDWARE_SAFETY_LATCH)

    # Closing admission alone is not enough: an operation can have acquired
    # the guard immediately before the latch.  Publish cancellation without
    # waiting for hardware I/O, and let each active executor perform its own
    # fail-closed stop path.  Imports stay local to avoid module cycles during
    # API startup.  These callbacks are best effort; the latch itself remains
    # authoritative even if a runtime is only partially initialized.
    try:
        from workflow.task_runtime import request_all_task_cancellations

        request_all_task_cancellations()
    except Exception:
        logger.exception("failed to broadcast task cancellation after hardware safety latch")
    try:
        from workflow.stage_reciprocation import stage_reciprocation_controller

        stage_reciprocation_controller.request_stop()
    except Exception:
        logger.exception("failed to request stage stop after hardware safety latch")
    _persist_hardware_safety_latch(latched)
    return hardware_safety_status()


def hardware_safety_status() -> Dict[str, Any]:
    with _HARDWARE_OPERATION_LOCK:
        if _HARDWARE_SAFETY_LATCH is None:
            return {
                "safe": True,
                "error_code": None,
                "message": None,
                "latched_at": None,
                "persistence_confirmed": True,
            }
        status = dict(_HARDWARE_SAFETY_LATCH)
        status["persistence_confirmed"] = bool(_HARDWARE_SAFETY_LATCH_PERSISTED)
        return status


def hardware_safety_is_clear() -> bool:
    with _HARDWARE_OPERATION_LOCK:
        return _HARDWARE_SAFETY_LATCH is None


def hardware_safety_fence_is_durable() -> bool:
    """Return true only when restart cannot erase the current safety state."""

    with _HARDWARE_OPERATION_LOCK:
        if _HARDWARE_SAFETY_LATCH is None:
            return True
        latch = dict(_HARDWARE_SAFETY_LATCH)
        path = _HARDWARE_SAFETY_LATCH_PATH
        persisted = _HARDWARE_SAFETY_LATCH_PERSISTED
    if path is not None and persisted and path.is_file():
        return True
    # The file may have been temporarily unavailable or incorrectly removed
    # while the API was still live. Recreate and verify it before allowing the
    # old process generation to release its single-worker fence.
    return _persist_hardware_safety_latch(latch)


def hardware_safety_shutdown_fence_ready() -> bool:
    """Finalize a clean generation or preserve an unsafe marker for restart.

    Callers must first prove every hardware runtime is quiescent. An unsafe
    latch is never removed. A clean write-ahead marker is deleted only while
    the API still owns its cross-process generation lock.
    """

    global _HARDWARE_SAFETY_GENERATION_ACTIVE, _HARDWARE_SAFETY_GENERATION_ID
    global _HARDWARE_SAFETY_LATCH_PERSISTED

    try:
        with _HARDWARE_SAFETY_PERSIST_LOCK:
            with _HARDWARE_OPERATION_LOCK:
                latch = (
                    None
                    if _HARDWARE_SAFETY_LATCH is None
                    else dict(_HARDWARE_SAFETY_LATCH)
                )
                path = _HARDWARE_SAFETY_LATCH_PATH
                generation_id = _HARDWARE_SAFETY_GENERATION_ID
                generation_active = _HARDWARE_SAFETY_GENERATION_ACTIVE
                admission_closed = _HARDWARE_ADMISSION_CLOSED
                if latch is not None:
                    return hardware_safety_fence_is_durable()
                if path is None or not generation_active:
                    return True
                if not admission_closed:
                    logger.critical(
                        "refusing to finalize hardware generation while admission is open"
                    )
                    return False

                marker = _validate_generation_marker(read_json_with_retry(path))
                if marker["generation_id"] != generation_id:
                    raise RuntimeError("active generation marker identity changed")
                path.unlink()
                if path.exists():
                    raise OSError("active generation marker still exists after removal")
                _HARDWARE_SAFETY_GENERATION_ACTIVE = False
                _HARDWARE_SAFETY_GENERATION_ID = None
                _HARDWARE_SAFETY_LATCH_PERSISTED = True
    except BaseException as exc:
        latch_hardware_unsafe(
            "HARDWARE_SAFETY_GENERATION_FINALIZE_FAILED",
            f"API 代际安全标记无法完成清理，禁止后续硬件操作: {type(exc).__name__}",
        )
        return hardware_safety_fence_is_durable()
    return True


def _reset_hardware_guard_for_tests(
    *,
    clear_persisted_latch: bool = False,
    forget_persistence_path: bool = False,
) -> None:
    """Reset process-local state only when persistence is test-isolated.

    This helper is deliberately private and refuses to run whenever its
    effective persistence path is the fixed production latch path. Therefore a
    production caller cannot use a convenient in-process function to erase the
    admission fence or its durable marker. Production recovery must use the
    audited offline operator tool while both process locks are held.
    """

    global _HARDWARE_OWNER, _CAMERA_RECORD_OWNER, _HARDWARE_SAFETY_LATCH
    global _HARDWARE_SAFETY_LATCH_PATH, _HARDWARE_SAFETY_LATCH_PERSISTED
    global _HARDWARE_SAFETY_GENERATION_ID, _HARDWARE_SAFETY_GENERATION_ACTIVE
    global _HARDWARE_ADMISSION_CLOSED

    with _HARDWARE_SAFETY_PERSIST_LOCK:
        with _HARDWARE_OPERATION_LOCK:
            path = _HARDWARE_SAFETY_LATCH_PATH
            effective_path = (
                _configured_hardware_safety_latch_path()
                if path is None
                else path.resolve(strict=False)
            )
            if effective_path == PRODUCTION_HARDWARE_SAFETY_LATCH_PATH:
                raise RuntimeError(
                    "test reset helper refuses to modify production hardware safety state"
                )
            _HARDWARE_OWNER = None
            _CAMERA_RECORD_OWNER = None
            _HARDWARE_SAFETY_LATCH = None
            _HARDWARE_SAFETY_LATCH_PERSISTED = False
            _HARDWARE_SAFETY_GENERATION_ID = None
            _HARDWARE_SAFETY_GENERATION_ACTIVE = False
            _HARDWARE_ADMISSION_CLOSED = False
            if forget_persistence_path:
                _HARDWARE_SAFETY_LATCH_PATH = None
        if clear_persisted_latch and path is not None:
            path.unlink(missing_ok=True)
