"""提供带重试和原子替换能力的文本与 JSON 文件读写工具。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


DEFAULT_IO_ATTEMPTS = 40
DEFAULT_IO_SLEEP_SEC = 0.05

_PATH_LOCKS_GUARD = threading.RLock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


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
            for _ in range(attempts):
                try:
                    os.replace(str(tmp), str(out_path))
                    return
                except OSError as exc:
                    last_exc = exc
                    time.sleep(sleep_s)
            if last_exc is not None:
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
    for _ in range(attempts):
        try:
            return in_path.read_text(encoding=encoding)
        except (OSError, UnicodeError) as exc:
            last_exc = exc
            time.sleep(sleep_s)
    if last_exc is not None:
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
    for _ in range(attempts):
        try:
            return json.loads(read_text_with_retry(path, attempts=1, sleep_s=sleep_s))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            last_exc = exc
            time.sleep(sleep_s)
    if last_exc is not None:
        raise last_exc
    return json.loads(Path(path).read_text(encoding="utf-8"))
