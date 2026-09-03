"""Shared log redaction helpers for API and file I/O diagnostics."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from workflow.path_guard import CONFIG_ROOT, DATA_ROOT, OUTPUTS_ROOT, PROJECT_ROOT


_TRUE_VALUES = {"1", "true", "yes", "on", "prod", "production"}
_FALSE_VALUES = {"0", "false", "no", "off", "debug", "development"}
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s,;)\]}'\"]+")
_POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z:])(/[^\s,;)\]}'\"<]+/[^\s,;)\]}'\"<]+)")
_TASK_ID_KV_RE = re.compile(r"(?i)\b(task_id)\s*=\s*(['\"]?)([^,\s;)}\]'\"]+)\2")
_TASK_ID_JSON_RE = re.compile(r"(?i)([\"']task_id[\"']\s*:\s*[\"'])([^\"']+)([\"'])")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return default


def log_redaction_enabled() -> bool:
    """Return whether sensitive log details should be redacted.

    Default is detailed logging for local/intranet debugging. Set
    COLONY_LOG_REDACT_SENSITIVE=1 in production to redact paths and task IDs.
    """
    return _env_bool("COLONY_LOG_REDACT_SENSITIVE", False)


def _path_roots() -> list[tuple[str, Path]]:
    return [
        ("<CONFIG_ROOT>", CONFIG_ROOT),
        ("<OUTPUTS_ROOT>", OUTPUTS_ROOT),
        ("<DATA_ROOT>", DATA_ROOT),
        ("<PROJECT_ROOT>", PROJECT_ROOT),
    ]


def _normalize_path_text(path: str | Path) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


def _redacted_path_label(raw_path: str) -> str:
    normalized = _normalize_path_text(raw_path)
    for label, root in _path_roots():
        root_text = _normalize_path_text(root.resolve(strict=False))
        if normalized == root_text or normalized.startswith(f"{root_text}/"):
            return f"{label}/<redacted>"
    return "<ABS_PATH>/<redacted>"


def _redact_paths(text: str) -> str:
    text = _WINDOWS_ABSOLUTE_PATH_RE.sub(lambda match: _redacted_path_label(match.group(0)), text)
    return _POSIX_ABSOLUTE_PATH_RE.sub(lambda match: _redacted_path_label(match.group(0)), text)


def _task_hash(task_id: str) -> str:
    digest = hashlib.sha256(task_id.encode("utf-8", errors="ignore")).hexdigest()[:8]
    return f"<task:{digest}>"


def _redact_task_ids(text: str) -> str:
    def replace_kv(match: re.Match[str]) -> str:
        key = match.group(1)
        quote = match.group(2) or ""
        return f"{key}={quote}{_task_hash(match.group(3))}{quote}"

    def replace_json(match: re.Match[str]) -> str:
        return f"{match.group(1)}{_task_hash(match.group(2))}{match.group(3)}"

    text = _TASK_ID_KV_RE.sub(replace_kv, text)
    return _TASK_ID_JSON_RE.sub(replace_json, text)


def sanitize_log_detail(detail: Any, *, redact: bool | None = None) -> str:
    """Return log detail text with optional path and task_id redaction."""
    text = "" if detail is None else str(detail)
    enabled = log_redaction_enabled() if redact is None else bool(redact)
    if not enabled:
        return text
    if _env_bool("COLONY_LOG_REDACT_PATHS", True):
        text = _redact_paths(text)
    if _env_bool("COLONY_LOG_REDACT_TASK_ID", True):
        text = _redact_task_ids(text)
    return text
