"""Prevent unsupported multi-process API deployments from sharing one hardware set."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Mapping

if os.name == "nt":
    import msvcrt
else:
    import fcntl


WORKER_COUNT_ENV_KEYS = ("COLONY_API_WORKERS", "UVICORN_WORKERS", "WEB_CONCURRENCY")

_LOCKED_PATHS: set[Path] = set()
_LOCKED_PATHS_LOCK = threading.RLock()


class SingleWorkerGuardError(RuntimeError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        log_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = str(error_code)
        self.message = str(message)
        self.log_detail = log_detail


def _parse_worker_count(name: str, value: object) -> int:
    text = str(value).strip()
    try:
        count = int(text)
    except (TypeError, ValueError) as exc:
        raise SingleWorkerGuardError(
            "INVALID_WORKER_COUNT",
            f"{name} 必须是正整数",
            log_detail=f"{name}={text!r}",
        ) from exc
    if count < 1:
        raise SingleWorkerGuardError(
            "INVALID_WORKER_COUNT",
            f"{name} 必须是正整数",
            log_detail=f"{name}={text!r}",
        )
    return count


def configured_worker_counts(environ: Mapping[str, object] | None = None) -> list[tuple[str, int]]:
    env = os.environ if environ is None else environ
    configured = []
    for key in WORKER_COUNT_ENV_KEYS:
        if key not in env:
            continue
        value = str(env[key]).strip()
        if not value:
            continue
        configured.append((key, _parse_worker_count(key, value)))
    return configured


def configured_worker_count(environ: Mapping[str, object] | None = None) -> tuple[str, int] | None:
    configured = configured_worker_counts(environ)
    return configured[0] if configured else None


def assert_single_worker_config(environ: Mapping[str, object] | None = None) -> tuple[str, int] | None:
    configured = configured_worker_counts(environ)
    if not configured:
        return None
    for name, count in configured:
        if count > 1:
            raise SingleWorkerGuardError(
                "MULTI_WORKER_NOT_SUPPORTED",
                "当前系统只控制一套硬件，API 服务只支持 1 个 worker",
                log_detail=f"{name}={count}",
            )
    return configured[0]


class SingleInstanceLock:
    """Cross-process non-blocking lock used to reject multiple API worker processes."""

    def __init__(self, path: str | Path, *, owner: str) -> None:
        self.path = Path(path)
        self.owner = str(owner)
        self._fh = None
        self._resolved_path: Path | None = None
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self) -> None:
        if self._acquired:
            return

        resolved = self.path.resolve(strict=False)
        with _LOCKED_PATHS_LOCK:
            if resolved in _LOCKED_PATHS:
                raise SingleWorkerGuardError(
                    "API_SERVER_ALREADY_RUNNING",
                    "检测到另一个 API worker/process 已经持有单 worker 锁",
                    log_detail=f"lock_path={resolved}",
                )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = self.path.open("a+b")
            try:
                self._lock_file(fh)
                self._write_owner(fh)
            except OSError as exc:
                fh.close()
                raise SingleWorkerGuardError(
                    "API_SERVER_ALREADY_RUNNING",
                    "检测到另一个 API worker/process 已经持有单 worker 锁",
                    log_detail=f"lock_path={resolved} owner={self.owner}",
                ) from exc
            except Exception:
                fh.close()
                raise

            self._fh = fh
            self._resolved_path = resolved
            self._acquired = True
            _LOCKED_PATHS.add(resolved)

    def release(self) -> None:
        if not self._acquired:
            return
        fh = self._fh
        resolved = self._resolved_path
        try:
            if fh is not None:
                try:
                    self._unlock_file(fh)
                except OSError:
                    pass
        finally:
            if fh is not None:
                fh.close()
            with _LOCKED_PATHS_LOCK:
                if resolved is not None:
                    _LOCKED_PATHS.discard(resolved)
            self._fh = None
            self._resolved_path = None
            self._acquired = False

    def _lock_file(self, fh) -> None:
        if os.name == "nt":
            fh.seek(0)
            if fh.read(1) == b"":
                fh.seek(0)
                fh.write(b"\0")
                fh.flush()
                os.fsync(fh.fileno())
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(self, fh) -> None:
        if os.name == "nt":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            return
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _write_owner(self, fh) -> None:
        payload = ((self.owner.strip() or "api-server") + os.linesep).encode("utf-8")[:255]
        payload = payload.ljust(256, b" ")
        fh.seek(1)
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
