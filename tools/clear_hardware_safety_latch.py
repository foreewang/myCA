"""Offline, auditable recovery for the persistent hardware safety latch.

This command is intentionally not part of the HTTP application. It can clear
the fixed production marker only while it exclusively owns both the API
generation lock and the camera-worker lock. The original marker is preserved
byte-for-byte and a clearance receipt records the operator decision.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from workflow.file_io import atomic_write_json, read_json_with_retry  # noqa: E402
from workflow.hardware_guard import (  # noqa: E402
    PRODUCTION_HARDWARE_SAFETY_LATCH_PATH,
)
from workflow.path_guard import DATA_ROOT  # noqa: E402
from workflow.process_guard import (  # noqa: E402
    SingleInstanceLock,
    SingleWorkerGuardError,
)


CONFIRMATION_PHRASE = "I CONFIRM ALL HARDWARE IS PHYSICALLY STOPPED"
PRODUCTION_API_LOCK_PATH = (DATA_ROOT / "api_server.lock").resolve(strict=False)
DEFAULT_CAMERA_WORKER_LOCK_PATH = (DATA_ROOT / "camera_worker.lock").resolve(
    strict=False
)
PRODUCTION_CLEARANCE_ARCHIVE_DIR = (
    DATA_ROOT / "hardware_safety_clearance_archive"
).resolve(strict=False)
MAX_MARKER_BYTES = 1024 * 1024
RECEIPT_VERSION = 1


class OfflineSafetyRecoveryError(RuntimeError):
    """A fail-closed rejection of an offline recovery attempt."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = str(error_code)
        self.message = str(message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _filename_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _production_camera_worker_lock_path() -> Path:
    """Use the same configured lock path as the camera worker.

    The production latch itself is never configurable. The worker lock already
    has a deployment environment override, so this offline command must run in
    the same service environment and acquire that exact effective lock.
    """

    raw = str(
        os.getenv(
            "COLONY_CAMERA_WORKER_LOCK_PATH",
            str(DEFAULT_CAMERA_WORKER_LOCK_PATH),
        )
    ).strip()
    path = Path(raw or DEFAULT_CAMERA_WORKER_LOCK_PATH)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve(strict=False)


def _validate_operator_input(
    *, operator: str, reason: str, confirmation: str
) -> tuple[str, str]:
    if confirmation != CONFIRMATION_PHRASE:
        raise OfflineSafetyRecoveryError(
            "CONFIRMATION_MISMATCH",
            f"--confirm must exactly equal: {CONFIRMATION_PHRASE}",
        )

    normalized_operator = str(operator).strip()
    normalized_reason = str(reason).strip()
    if not normalized_operator:
        raise OfflineSafetyRecoveryError(
            "OPERATOR_REQUIRED",
            "--operator is required and cannot be blank",
        )
    if len(normalized_operator) > 128 or any(
        ord(character) < 32 for character in normalized_operator
    ):
        raise OfflineSafetyRecoveryError(
            "OPERATOR_INVALID",
            "--operator must be a single printable line of at most 128 characters",
        )
    if not normalized_reason:
        raise OfflineSafetyRecoveryError(
            "REASON_REQUIRED",
            "--reason is required and cannot be blank",
        )
    if len(normalized_reason) > 2000:
        raise OfflineSafetyRecoveryError(
            "REASON_INVALID",
            "--reason must be at most 2000 characters",
        )
    return normalized_operator, normalized_reason


def _read_marker_bytes(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise OfflineSafetyRecoveryError(
            "SAFETY_LATCH_NOT_FOUND",
            f"no hardware safety marker exists at the fixed path: {path}",
        ) from exc
    except OSError as exc:
        raise OfflineSafetyRecoveryError(
            "SAFETY_LATCH_READ_FAILED",
            f"cannot inspect hardware safety marker: {type(exc).__name__}",
        ) from exc

    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise OfflineSafetyRecoveryError(
            "SAFETY_LATCH_NOT_REGULAR_FILE",
            "hardware safety marker must be a regular file and not a symbolic link",
        )
    if metadata.st_size <= 0 or metadata.st_size > MAX_MARKER_BYTES:
        raise OfflineSafetyRecoveryError(
            "SAFETY_LATCH_SIZE_INVALID",
            f"hardware safety marker size must be 1..{MAX_MARKER_BYTES} bytes",
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise OfflineSafetyRecoveryError(
            "SAFETY_LATCH_READ_FAILED",
            f"cannot read hardware safety marker: {type(exc).__name__}",
        ) from exc
    if len(data) != metadata.st_size:
        raise OfflineSafetyRecoveryError(
            "SAFETY_LATCH_CHANGED",
            "hardware safety marker changed while it was being inspected",
        )
    return data


def _marker_summary(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        return {
            "parse_status": "invalid",
            "parse_error": type(exc).__name__,
        }
    if not isinstance(payload, dict):
        return {
            "parse_status": "invalid",
            "parse_error": "marker JSON is not an object",
        }
    summary: dict[str, Any] = {"parse_status": "valid_json_object"}
    for key in (
        "version",
        "record_type",
        "latch_id",
        "generation_id",
        "error_code",
        "latched_at",
        "started_at",
        "pid",
    ):
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value
    return summary


def _write_exclusive_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd: int | None = None
    try:
        fd = os.open(str(path), flags, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("exclusive audit write made no progress")
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        if fd is not None:
            os.close(fd)
            fd = None
        path.unlink(missing_ok=True)
        raise
    finally:
        if fd is not None:
            os.close(fd)


def _write_exclusive_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    _write_exclusive_bytes(path, encoded)


def _verify_receipt(path: Path, receipt_id: str, status_value: str) -> None:
    try:
        payload = read_json_with_retry(path)
    except Exception as exc:
        raise OfflineSafetyRecoveryError(
            "CLEARANCE_RECEIPT_VERIFY_FAILED",
            f"cannot read back clearance receipt: {type(exc).__name__}",
        ) from exc
    if not isinstance(payload, dict) or (
        payload.get("receipt_id") != receipt_id
        or payload.get("status") != status_value
    ):
        raise OfflineSafetyRecoveryError(
            "CLEARANCE_RECEIPT_VERIFY_FAILED",
            "clearance receipt identity or status did not read back exactly",
        )


def _fsync_directory(path: Path) -> None:
    # Windows does not support opening a directory through os.open. The marker,
    # archive, and receipt files themselves are fsynced on every write.
    if os.name == "nt":
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _clear_hardware_safety_latch_with_paths(
    *,
    operator: str,
    reason: str,
    confirmation: str,
    latch_path: Path,
    api_lock_path: Path,
    camera_worker_lock_path: Path,
    archive_dir: Path,
) -> dict[str, Any]:
    """Implementation seam for tests; production callers use the fixed wrapper."""

    normalized_operator, normalized_reason = _validate_operator_input(
        operator=operator,
        reason=reason,
        confirmation=confirmation,
    )
    latch_path = Path(latch_path).resolve(strict=False)
    api_lock_path = Path(api_lock_path).resolve(strict=False)
    camera_worker_lock_path = Path(camera_worker_lock_path).resolve(strict=False)
    archive_dir = Path(archive_dir).resolve(strict=False)

    operation_id = uuid.uuid4().hex
    lock_owner = f"offline-hardware-safety-recovery pid={os.getpid()} id={operation_id}"
    api_lock = SingleInstanceLock(api_lock_path, owner=lock_owner)
    camera_lock = SingleInstanceLock(camera_worker_lock_path, owner=lock_owner)

    try:
        try:
            api_lock.acquire()
        except SingleWorkerGuardError as exc:
            raise OfflineSafetyRecoveryError(
                "API_PROCESS_LOCK_HELD",
                "refusing clearance because the API process lock is held",
            ) from exc
        try:
            camera_lock.acquire()
        except SingleWorkerGuardError as exc:
            raise OfflineSafetyRecoveryError(
                "CAMERA_WORKER_PROCESS_LOCK_HELD",
                "refusing clearance because the camera worker lock is held",
            ) from exc

        marker_bytes = _read_marker_bytes(latch_path)
        marker_sha256 = hashlib.sha256(marker_bytes).hexdigest()
        filename_prefix = f"{_filename_timestamp()}-{operation_id}"
        archive_path = archive_dir / f"hardware_safety_latch-{filename_prefix}.marker"
        receipt_path = archive_dir / f"clearance_receipt-{filename_prefix}.json"

        # Prepare a byte-exact durable archive and an audit receipt before the
        # active marker can disappear. If the process crashes before the move,
        # the marker remains fenced. If it crashes after the move, the prepared
        # receipt and archived original explain the interrupted clearance.
        _write_exclusive_bytes(archive_path, marker_bytes)
        if archive_path.read_bytes() != marker_bytes:
            raise OfflineSafetyRecoveryError(
                "SAFETY_LATCH_ARCHIVE_VERIFY_FAILED",
                "archived safety marker did not read back byte-for-byte",
            )

        receipt: dict[str, Any] = {
            "version": RECEIPT_VERSION,
            "record_type": "hardware_safety_clearance_receipt",
            "receipt_id": operation_id,
            "status": "prepared",
            "requested_at": _utc_now(),
            "cleared_at": None,
            "operator": normalized_operator,
            "reason": normalized_reason,
            "confirmation_phrase": CONFIRMATION_PHRASE,
            "host": socket.gethostname(),
            "process_id": os.getpid(),
            "latch_path": str(latch_path),
            "api_lock_path": str(api_lock_path),
            "camera_worker_lock_path": str(camera_worker_lock_path),
            "archive_path": str(archive_path),
            "receipt_path": str(receipt_path),
            "original_marker_sha256": marker_sha256,
            "original_marker_size": len(marker_bytes),
            "original_marker": _marker_summary(marker_bytes),
        }
        _write_exclusive_json(receipt_path, receipt)
        _verify_receipt(receipt_path, operation_id, "prepared")

        if _read_marker_bytes(latch_path) != marker_bytes:
            raise OfflineSafetyRecoveryError(
                "SAFETY_LATCH_CHANGED",
                "hardware safety marker changed after archival; it was not cleared",
            )

        try:
            os.replace(str(latch_path), str(archive_path))
            _fsync_directory(archive_dir)
        except OSError as exc:
            raise OfflineSafetyRecoveryError(
                "SAFETY_LATCH_CLEAR_FAILED",
                f"failed to archive and remove active marker: {type(exc).__name__}",
            ) from exc
        if latch_path.exists() or archive_path.read_bytes() != marker_bytes:
            raise OfflineSafetyRecoveryError(
                "SAFETY_LATCH_CLEAR_VERIFY_FAILED",
                "active marker removal or byte-exact archive verification failed",
            )

        receipt["status"] = "cleared"
        receipt["cleared_at"] = _utc_now()
        try:
            atomic_write_json(receipt_path, receipt)
            _verify_receipt(receipt_path, operation_id, "cleared")
            _fsync_directory(archive_dir)
        except OfflineSafetyRecoveryError:
            raise
        except Exception as exc:
            raise OfflineSafetyRecoveryError(
                "CLEARANCE_RECEIPT_FINALIZE_FAILED",
                f"marker was archived but receipt finalization failed: {type(exc).__name__}",
            ) from exc

        return receipt
    finally:
        camera_lock.release()
        api_lock.release()


def clear_production_hardware_safety_latch(
    *, operator: str, reason: str, confirmation: str
) -> dict[str, Any]:
    """Clear only the fixed production marker through the offline protocol."""

    return _clear_hardware_safety_latch_with_paths(
        operator=operator,
        reason=reason,
        confirmation=confirmation,
        latch_path=PRODUCTION_HARDWARE_SAFETY_LATCH_PATH,
        api_lock_path=PRODUCTION_API_LOCK_PATH,
        camera_worker_lock_path=_production_camera_worker_lock_path(),
        archive_dir=PRODUCTION_CLEARANCE_ARCHIVE_DIR,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline clearance of the persistent hardware safety latch. "
            "The API and camera worker must both be fully stopped."
        )
    )
    parser.add_argument("--operator", required=True, help="operator name or employee ID")
    parser.add_argument("--reason", required=True, help="verified fault disposition")
    parser.add_argument(
        "--confirm",
        required=True,
        help=f'exact phrase required: "{CONFIRMATION_PHRASE}"',
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        receipt = clear_production_hardware_safety_latch(
            operator=args.operator,
            reason=args.reason,
            confirmation=args.confirm,
        )
    except OfflineSafetyRecoveryError as exc:
        print(
            json.dumps(
                {"ok": False, "error_code": exc.error_code, "message": exc.message},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "receipt_id": receipt["receipt_id"],
                "archive_path": receipt["archive_path"],
                "receipt_path": receipt["receipt_path"],
                "status": receipt["status"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
