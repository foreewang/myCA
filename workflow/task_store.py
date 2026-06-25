"""
负责 API 任务记录的持久化、读取、更新、恢复和任务记录结构构建
任务记录文件路径管理
任务记录读写
任务状态恢复
任务状态标记
任务记录结构构建
任务存储异常
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from workflow.file_io import atomic_write_json, read_json_with_retry


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TASK_INDEX_DIR = PROJECT_ROOT / "data" / "task_index"

TASK_RECORD_IO_LOCK = threading.RLock()
TASK_RECORD_REPLACE_ATTEMPTS = 200
TASK_RECORD_REPLACE_SLEEP_SEC = 0.05
TASK_ACTIVE_STATUSES = {"queued", "running"}
TASK_TERMINAL_STATUSES = {"success", "failed", "interrupted", "canceled"}

logger = logging.getLogger(__name__)


class TaskStoreError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        *,
        log_detail: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.error_code = str(error_code)
        self.message = str(message)
        self.log_detail = log_detail
        self.cause = cause


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_str_path(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def task_index_dir() -> Path:
    raw = os.getenv("TASK_INDEX_DIR")
    return Path(raw) if raw else DEFAULT_TASK_INDEX_DIR


def sanitize_task_id(task_id: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(task_id).strip())
    if not sanitized:
        raise ValueError("非法 task_id")
    return sanitized


def task_record_path(task_id: str) -> Path:
    task_id = sanitize_task_id(task_id)
    index_dir = task_index_dir()
    index_dir.mkdir(parents=True, exist_ok=True)
    return index_dir / f"{task_id}.json"


def write_task_record(record: Dict[str, Any]) -> None:
    with TASK_RECORD_IO_LOCK:
        write_task_record_unlocked(record)


def write_task_record_unlocked(record: Dict[str, Any]) -> None:
    path = task_record_path(record["task_id"])
    atomic_write_json(
        path,
        record,
        attempts=TASK_RECORD_REPLACE_ATTEMPTS,
        sleep_s=TASK_RECORD_REPLACE_SLEEP_SEC,
    )


def read_task_record(task_id: str) -> Dict[str, Any]:
    with TASK_RECORD_IO_LOCK:
        return read_task_record_unlocked(task_id)


def read_task_record_unlocked(task_id: str) -> Dict[str, Any]:
    path = task_record_path(task_id)
    if not path.exists():
        raise TaskStoreError(
            404,
            "TASK_NOT_FOUND",
            "未找到任务记录",
            log_detail=f"task_id={task_id} path={path}",
        )
    try:
        record = read_json_with_retry(
            path,
            attempts=TASK_RECORD_REPLACE_ATTEMPTS,
            sleep_s=TASK_RECORD_REPLACE_SLEEP_SEC,
        )
    except Exception as exc:
        raise TaskStoreError(
            503,
            "TASK_RECORD_TEMPORARILY_UNREADABLE",
            "任务记录暂时不可读，请稍后重试",
            log_detail=f"task_id={task_id} path={path}",
            cause=exc,
        ) from exc
    if not isinstance(record, dict):
        raise TaskStoreError(
            500,
            "TASK_RECORD_INVALID",
            "任务记录格式异常，请查看本地日志",
            log_detail=f"task_id={task_id} path={path}",
        )
    return record


def task_exists(task_id: str) -> bool:
    return task_record_path(task_id).exists()


def mark_record_interrupted(record: Dict[str, Any], reason: str) -> Dict[str, Any]:
    now = utc_now()
    previous_status = record.get("status")
    updated = dict(record)
    updated["status"] = "interrupted"
    updated["previous_status"] = previous_status
    updated["updated_at"] = now
    updated["finished_at"] = now
    updated["interrupted_at"] = now
    updated["message"] = reason
    updated["error"] = reason

    wells = updated.get("wells")
    if isinstance(wells, dict):
        updated_wells = {}
        for well_name, well_record in wells.items():
            if not isinstance(well_record, dict):
                updated_wells[well_name] = well_record
                continue
            item = dict(well_record)
            if item.get("status") in TASK_ACTIVE_STATUSES:
                item["previous_status"] = item.get("status")
                item["status"] = "interrupted"
                item["message"] = reason
            updated_wells[well_name] = item
        updated["wells"] = updated_wells

    return updated


def mark_record_canceled(record: Dict[str, Any], reason: str) -> Dict[str, Any]:
    now = utc_now()
    previous_status = record.get("status")
    updated = dict(record)
    updated["status"] = "canceled"
    updated["previous_status"] = previous_status
    updated["cancel_requested"] = True
    updated.setdefault("cancel_requested_at", now)
    updated["canceled_at"] = now
    updated["finished_at"] = now
    updated["updated_at"] = now
    updated["message"] = reason
    updated["cancel_reason"] = reason

    wells = updated.get("wells")
    if isinstance(wells, dict):
        updated_wells = {}
        for well_name, well_record in wells.items():
            if not isinstance(well_record, dict):
                updated_wells[well_name] = well_record
                continue
            item = dict(well_record)
            if item.get("status") in TASK_ACTIVE_STATUSES:
                item["previous_status"] = item.get("status")
                item["status"] = "canceled"
                item["message"] = reason
            updated_wells[well_name] = item
        updated["wells"] = updated_wells

    return updated


def recover_interrupted_task_records() -> Dict[str, int]:
    reason = "API 服务启动时发现任务未正常结束，已标记为 interrupted"
    stats = {
        "scanned": 0,
        "interrupted": 0,
        "errors": 0,
    }
    index_dir = task_index_dir()
    if not index_dir.exists():
        return stats

    with TASK_RECORD_IO_LOCK:
        for path in sorted(index_dir.glob("*.json")):
            stats["scanned"] += 1
            try:
                record = read_json_with_retry(path)
                if not isinstance(record, dict):
                    continue
                if record.get("status") not in TASK_ACTIVE_STATUSES:
                    continue
                if not str(record.get("task_id") or "").strip():
                    record["task_id"] = path.stem
                write_task_record_unlocked(mark_record_interrupted(record, reason))
                stats["interrupted"] += 1
            except Exception:
                stats["errors"] += 1
                logger.exception("failed to recover interrupted task record: %s", path)

    if stats["interrupted"] or stats["errors"]:
        logger.warning(
            "task record startup recovery finished: scanned=%s interrupted=%s errors=%s",
            stats["scanned"],
            stats["interrupted"],
            stats["errors"],
        )
    return stats


def update_task_record(task_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    with TASK_RECORD_IO_LOCK:
        record = read_task_record_unlocked(task_id)
        record.update(patch)
        record["updated_at"] = utc_now()
        write_task_record_unlocked(record)
        return record


def _first_saved_image_dir(capture_result: Dict[str, Any] | None) -> str | None:
    if not isinstance(capture_result, dict):
        return None
    captures = capture_result.get("captures") or []
    if not captures:
        return None
    first = captures[0] or {}
    capture_info = first.get("capture_result") or {}
    saved_path = capture_info.get("saved_path")
    if not saved_path:
        return None
    return str(Path(saved_path).parent)


def _normalize_wells(task: Dict[str, Any]) -> list[str]:
    observe_scope = str(task.get("observe_scope") or "").lower()
    target = task.get("target", {}) or {}
    if observe_scope == "well_list":
        return [str(w).strip() for w in (target.get("well_list") or []) if str(w).strip()]
    if observe_scope == "single_well":
        well_name = str(task.get("well_name") or target.get("well_name") or "").strip()
        return [well_name] if well_name else []
    return []


def guess_well_artifacts_from_task(task: Dict[str, Any]) -> Dict[str, Any]:
    observe_scope = str(task.get("observe_scope") or "").lower()
    capture_cfg = task.get("capture", {}) or {}
    detect_cfg = task.get("detect", {}) or {}
    output_cfg = task.get("output", {}) or {}
    comp_cfg = task.get("compensate", {}) or {}
    save_dir = safe_str_path(capture_cfg.get("save_dir"))
    detect_output_json = safe_str_path(detect_cfg.get("output_json")) or safe_str_path(output_cfg.get("detect_json"))
    compensate_output_json = safe_str_path(comp_cfg.get("output_json")) or safe_str_path(output_cfg.get("compensate_json"))

    wells: Dict[str, Any] = {}
    if observe_scope in {"well_list", "full_plate"}:
        for well_name in _normalize_wells(task):
            base = Path(save_dir) / well_name if save_dir else None
            wells[well_name] = {
                "image_dir": str(base / "images") if base else None,
                "capture_result_json": str(base / "scan_result.json") if base else None,
                "detect_result_json": str(base / "detect_result.json") if base else None,
                "compensate_result_json": str(base / "compensate_result.json") if base else None,
                "status": "queued",
                "progress": 0,
                "message": "waiting",
            }
        return wells

    well_name = ""
    if observe_scope == "single_well":
        well_name = str(task.get("well_name") or (task.get("target", {}) or {}).get("well_name") or "").strip()
    if not well_name:
        return {}

    wells[well_name] = {
        "image_dir": save_dir,
        "capture_result_json": None,
        "detect_result_json": detect_output_json,
        "compensate_result_json": compensate_output_json,
        "status": "queued",
        "progress": 0,
        "message": "waiting",
    }
    return wells


def build_well_artifacts_from_result(result: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
    observe_scope = str(result.get("observe_scope") or "").lower()
    task_output = task.get("output", {}) or {}
    task_detect = task.get("detect", {}) or {}
    task_comp = task.get("compensate", {}) or {}
    task_capture = task.get("capture", {}) or {}

    if observe_scope in {"well_list", "full_plate"}:
        wells_info = {}
        for item in result.get("wells", []) or []:
            well_name = str(item.get("well_name") or "").strip()
            if not well_name:
                continue
            capture_result_json = safe_str_path(item.get("capture_result_json"))
            detect_result_json = safe_str_path(item.get("detect_result_json"))
            compensate_result_json = safe_str_path(item.get("compensate_result_json"))
            image_dir = None
            if capture_result_json:
                image_dir = str(Path(capture_result_json).parent / "images")
            wells_info[well_name] = {
                "image_dir": image_dir,
                "capture_result_json": capture_result_json,
                "detect_result_json": detect_result_json,
                "compensate_result_json": compensate_result_json,
                "status": "success",
                "progress": 100,
                "message": "completed",
            }
        return wells_info

    well_name = str(result.get("well_name") or task.get("well_name") or (task.get("target", {}) or {}).get("well_name") or "").strip()
    if not well_name:
        return {}

    image_dir = None
    capture_result_json = None
    detect_result_json = safe_str_path(task_detect.get("output_json")) or safe_str_path(task_output.get("detect_json"))
    compensate_result_json = safe_str_path(task_comp.get("output_json")) or safe_str_path(task_output.get("compensate_json"))

    if result.get("capture_result"):
        image_dir = _first_saved_image_dir(result.get("capture_result"))

    if image_dir is None:
        save_dir = safe_str_path(task_capture.get("save_dir"))
        if save_dir:
            image_dir = save_dir

    return {
        well_name: {
            "image_dir": image_dir,
            "capture_result_json": capture_result_json,
            "detect_result_json": detect_result_json,
            "compensate_result_json": compensate_result_json,
            "status": "success",
            "progress": 100,
            "message": "completed",
        }
    }


def build_task_record(task: Dict[str, Any], result: Dict[str, Any], dump_json: str | None, persist_result: bool) -> Dict[str, Any]:
    task_id = str(result.get("task_id") or task.get("task_id") or "")
    output_cfg = task.get("output", {}) or {}
    result_json_path = safe_str_path(dump_json) or safe_str_path(output_cfg.get("result_json"))

    return {
        "task_id": task_id,
        "status": result.get("status"),
        "task_type": result.get("task_type"),
        "observe_scope": result.get("observe_scope"),
        "plate_type": result.get("plate_type"),
        "objective_name": result.get("objective_name"),
        "stored_at_utc": utc_now(),
        "created_at": utc_now(),
        "started_at": None,
        "updated_at": utc_now(),
        "finished_at": utc_now(),
        "persist_result": bool(persist_result),
        "result_json_path": result_json_path,
        "base_save_dir": safe_str_path(result.get("base_save_dir")),
        "progress": 100,
        "message": "task completed",
        "current_stage": None,
        "current_well": None,
        "wells": build_well_artifacts_from_result(result, task),
        "result": result,
        "request_task": task,
    }


def build_failed_record(
    task: Dict[str, Any],
    error: str,
    dump_json: str | None,
    persist_result: bool,
    *,
    error_code: str = "TASK_EXECUTION_FAILED",
) -> Dict[str, Any]:
    task_id = str(task.get("task_id") or "")
    output_cfg = task.get("output", {}) or {}
    return {
        "task_id": task_id,
        "status": "failed",
        "task_type": task.get("task_type"),
        "observe_scope": task.get("observe_scope"),
        "plate_type": task.get("plate_type"),
        "objective_name": task.get("objective"),
        "stored_at_utc": utc_now(),
        "created_at": utc_now(),
        "started_at": None,
        "updated_at": utc_now(),
        "finished_at": utc_now(),
        "persist_result": bool(persist_result),
        "result_json_path": safe_str_path(dump_json) or safe_str_path(output_cfg.get("result_json")),
        "base_save_dir": None,
        "progress": 100,
        "message": error,
        "current_stage": None,
        "current_well": None,
        "wells": guess_well_artifacts_from_task(task),
        "error_code": error_code,
        "error": error,
        "request_task": task,
    }


def build_accepted_record(task: Dict[str, Any], dump_json: str | None, persist_result: bool) -> Dict[str, Any]:
    output_cfg = task.get("output", {}) or {}
    return {
        "task_id": str(task.get("task_id") or ""),
        "status": "queued",
        "task_type": task.get("task_type"),
        "observe_scope": task.get("observe_scope"),
        "plate_type": task.get("plate_type"),
        "objective_name": task.get("objective"),
        "stored_at_utc": None,
        "created_at": utc_now(),
        "started_at": None,
        "updated_at": utc_now(),
        "finished_at": None,
        "persist_result": bool(persist_result),
        "result_json_path": safe_str_path(dump_json) or safe_str_path(output_cfg.get("result_json")),
        "base_save_dir": safe_str_path((task.get("capture", {}) or {}).get("save_dir")),
        "progress": 0,
        "message": "task accepted",
        "current_stage": None,
        "current_well": None,
        "wells": guess_well_artifacts_from_task(task),
        "result": None,
        "request_task": task,
    }
