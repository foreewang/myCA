"""提供带重试和原子替换能力的文本与 JSON 文件读写工具。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from workflow.log_sanitizer import sanitize_log_detail


DEFAULT_IO_ATTEMPTS = 40
DEFAULT_IO_SLEEP_SEC = 0.05
DEFAULT_SLOW_IO_WARNING_MS = 500.0

_PATH_LOCKS_GUARD = threading.RLock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
logger = logging.getLogger(__name__)


def _slow_io_warning_threshold_ms() -> float:
    raw = os.getenv("COLONY_FILE_IO_SLOW_WARNING_MS")
    if raw is None:
        return DEFAULT_SLOW_IO_WARNING_MS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_SLOW_IO_WARNING_MS


def _path_kind(path: Path) -> str:
    parts = {part.lower() for part in path.parts}
    suffix = path.suffix.lower()
    if "task_index" in parts:
        return "task_record"
    if suffix == ".json":
        return "json"
    if suffix in {".yaml", ".yml"}:
        return "config"
    if suffix in {".txt", ".log"}:
        return "text"
    return "file"


def _log_slow_retry(
    op: str,
    path: Path,
    started: float,
    attempts_used: int,
    last_exc: BaseException | None,
) -> None:
    if attempts_used <= 1:
        return
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    threshold_ms = _slow_io_warning_threshold_ms()
    if elapsed_ms < threshold_ms:
        return
    error_name = type(last_exc).__name__ if last_exc is not None else ""
    logger.warning(
        "FILE_IO_SLOW: op=%s path_kind=%s path=%s elapsed_ms=%.1f attempts=%s last_error=%s",
        op,
        _path_kind(path),
        sanitize_log_detail(str(path.resolve(strict=False))),
        elapsed_ms,
        attempts_used,
        error_name,
    )


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve(strict=False))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    attempts: int = DEFAULT_IO_ATTEMPTS,
    sleep_s: float = DEFAULT_IO_SLEEP_SEC,
    _op_name: str = "atomic_write_text",
) -> None:
    """Write text through a same-directory temp file and atomic replacement.

    This prevents readers from seeing a partially written JSON/text file. The
    retry loop handles short Windows handle contention from readers, antivirus,
    or file indexers.
    """
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    attempts = max(1, int(attempts))
    sleep_s = max(0.0, float(sleep_s))
    tmp = out_path.with_name(
        f".{out_path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )

    with _path_lock(out_path):
        try:
            with tmp.open("w", encoding=encoding) as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())

            last_exc: OSError | None = None
            replace_started = time.perf_counter()
            for attempt_no in range(1, attempts + 1):
                try:
                    os.replace(str(tmp), str(out_path))
                    _log_slow_retry(_op_name, out_path, replace_started, attempt_no, last_exc)
                    return
                except OSError as exc:
                    last_exc = exc
                    time.sleep(sleep_s)
            if last_exc is not None:
                _log_slow_retry(_op_name, out_path, replace_started, attempts, last_exc)
                raise last_exc
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


def atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    ensure_ascii: bool = False,
    indent: int | None = 2,
    attempts: int = DEFAULT_IO_ATTEMPTS,
    sleep_s: float = DEFAULT_IO_SLEEP_SEC,
) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=ensure_ascii, indent=indent),
        attempts=attempts,
        sleep_s=sleep_s,
        _op_name="atomic_write_json",
    )


def read_text_with_retry(
    path: str | Path,
    *,
    encoding: str = "utf-8",
    attempts: int = DEFAULT_IO_ATTEMPTS,
    sleep_s: float = DEFAULT_IO_SLEEP_SEC,
) -> str:
    in_path = Path(path)
    attempts = max(1, int(attempts))
    sleep_s = max(0.0, float(sleep_s))
    last_exc: OSError | UnicodeError | None = None
    started = time.perf_counter()
    for attempt_no in range(1, attempts + 1):
        try:
            text = in_path.read_text(encoding=encoding)
            _log_slow_retry("read_text", in_path, started, attempt_no, last_exc)
            return text
        except (OSError, UnicodeError) as exc:
            last_exc = exc
            time.sleep(sleep_s)
    if last_exc is not None:
        _log_slow_retry("read_text", in_path, started, attempts, last_exc)
        raise last_exc
    return in_path.read_text(encoding=encoding)


def read_json_with_retry(
    path: str | Path,
    *,
    attempts: int = DEFAULT_IO_ATTEMPTS,
    sleep_s: float = DEFAULT_IO_SLEEP_SEC,
) -> Any:
    attempts = max(1, int(attempts))
    sleep_s = max(0.0, float(sleep_s))
    last_exc: OSError | UnicodeError | json.JSONDecodeError | None = None
    in_path = Path(path)
    started = time.perf_counter()
    for attempt_no in range(1, attempts + 1):
        try:
            payload = json.loads(read_text_with_retry(in_path, attempts=1, sleep_s=sleep_s))
            _log_slow_retry("read_json", in_path, started, attempt_no, last_exc)
            return payload
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            last_exc = exc
            time.sleep(sleep_s)
    if last_exc is not None:
        _log_slow_retry("read_json", in_path, started, attempts, last_exc)
        raise last_exc
    return json.loads(in_path.read_text(encoding="utf-8"))
