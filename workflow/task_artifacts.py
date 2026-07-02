"""解析任务结果、孔位图片目录和下载文件，为 API 提供产物查询响应。"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Callable, Dict, Iterable

from workflow.file_io import read_json_with_retry
from workflow.path_guard import resolve_output_path, safe_str_path

IMAGE_SUFFIXES = {".bmp", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
DEFAULT_IMAGE_LIMIT = 100
MAX_IMAGE_LIMIT = 1000


class TaskArtifactError(RuntimeError):
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
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.log_detail = log_detail
        self.cause = cause


def ensure_well_record(record: Dict[str, Any], well_name: str) -> Dict[str, Any]:
    wells = record.get("wells") or {}
    if well_name not in wells:
        raise TaskArtifactError(
            404,
            "WELL_NOT_FOUND",
            "未找到指定孔位记录",
            log_detail=f"task_id={record.get('task_id')} well_name={well_name}",
        )
    return wells[well_name]


def resolve_image_dir(record: Dict[str, Any], well_name: str) -> Path:
    well_record = ensure_well_record(record, well_name)
    image_dir = well_record.get("image_dir")
    if not image_dir:
        raise TaskArtifactError(
            404,
            "IMAGE_DIR_NOT_RECORDED",
            "当前孔位未记录图片目录",
            log_detail=f"task_id={record.get('task_id')} well_name={well_name}",
        )
    path = Path(resolve_output_path(image_dir, f"task.{record.get('task_id')}.wells.{well_name}.image_dir"))
    if not path.exists() or not path.is_dir():
        raise TaskArtifactError(
            404,
            "IMAGE_DIR_NOT_FOUND",
            "图片目录不存在",
            log_detail=f"task_id={record.get('task_id')} well_name={well_name} path={path}",
        )
    return path


def existing_output_path_or_none(value: Any, field_name: str) -> str | None:
    raw = safe_str_path(value)
    if raw is None:
        return None
    path = Path(resolve_output_path(raw, field_name))
    return str(path) if path.exists() else None


def count_images(image_dir: Path) -> int:
    if not image_dir.exists() or not image_dir.is_dir():
        return 0
    return sum(1 for p in image_dir.iterdir() if is_image_file(p))


def is_incomplete_artifact(path: Path) -> bool:
    name = path.name.lower()
    return path.suffix.lower() == ".part" or ".part." in name


def is_image_file(path: Path) -> bool:
    return path.is_file() and not is_incomplete_artifact(path) and path.suffix.lower() in IMAGE_SUFFIXES


def list_image_names(image_dir: Path) -> list[str]:
    return sorted(p.name for p in image_dir.iterdir() if is_image_file(p))


def _normalize_pagination(limit: int | None, offset: int | None) -> tuple[int, int]:
    try:
        normalized_limit = int(limit if limit is not None else DEFAULT_IMAGE_LIMIT)
    except Exception:
        normalized_limit = DEFAULT_IMAGE_LIMIT
    try:
        normalized_offset = int(offset if offset is not None else 0)
    except Exception:
        normalized_offset = 0
    normalized_limit = max(1, min(MAX_IMAGE_LIMIT, normalized_limit))
    normalized_offset = max(0, normalized_offset)
    return normalized_limit, normalized_offset


def build_task_result_response(
    record: Dict[str, Any],
    active_statuses: Iterable[str],
    *,
    json_reader: Callable[..., Any] = read_json_with_retry,
) -> Dict[str, Any]:
    if record.get("status") in active_statuses:
        return {
            "task_id": record.get("task_id"),
            "status": record.get("status"),
            "objective_name": record.get("objective_name"),
            "progress": record.get("progress", 0),
            "message": record.get("message"),
            "current_stage": record.get("current_stage"),
            "current_well": record.get("current_well"),
            "result_json_path": record.get("result_json_path"),
            "result": None,
        }

    task_id = record.get("task_id")
    result_json_path = record.get("result_json_path")
    if result_json_path:
        path = Path(resolve_output_path(result_json_path, f"task.{task_id}.result_json_path"))
        if path.exists() and path.is_file():
            try:
                result = json_reader(path)
            except Exception as exc:
                raise TaskArtifactError(
                    503,
                    "RESULT_JSON_TEMPORARILY_UNREADABLE",
                    "结果文件暂时不可读，请稍后重试",
                    log_detail=f"task_id={task_id} path={path}",
                    cause=exc,
                ) from exc
            if not isinstance(result, dict):
                raise TaskArtifactError(
                    500,
                    "RESULT_JSON_INVALID",
                    "结果文件格式异常，请查看本地日志",
                    log_detail=f"task_id={task_id} path={path}",
                )
            return result

    result = record.get("result")
    if result is not None:
        return result
    return record


def build_well_images_response(
    record: Dict[str, Any],
    well_name: str,
    *,
    limit: int | None = DEFAULT_IMAGE_LIMIT,
    offset: int | None = 0,
) -> Dict[str, Any]:
    task_id = record.get("task_id")
    image_dir = resolve_image_dir(record, well_name)
    well_record = ensure_well_record(record, well_name)
    capture_path = well_record.get("capture_result_json")
    detect_path = well_record.get("detect_result_json")
    compensate_path = well_record.get("compensate_result_json")
    normalized_limit, normalized_offset = _normalize_pagination(limit, offset)
    image_names = list_image_names(image_dir)
    page_images = image_names[normalized_offset : normalized_offset + normalized_limit]

    return {
        "task_id": task_id,
        "well_name": well_name,
        "image_dir": str(image_dir),
        "capture_result_json": existing_output_path_or_none(capture_path, f"task.{task_id}.{well_name}.capture_result_json"),
        "detect_result_json": existing_output_path_or_none(detect_path, f"task.{task_id}.{well_name}.detect_result_json"),
        "compensate_result_json": existing_output_path_or_none(
            compensate_path,
            f"task.{task_id}.{well_name}.compensate_result_json",
        ),
        "images": page_images,
        "total": len(image_names),
        "limit": normalized_limit,
        "offset": normalized_offset,
        "has_more": normalized_offset + normalized_limit < len(image_names),
    }


def resolve_well_image_file(record: Dict[str, Any], well_name: str, filename: str) -> tuple[Path, str]:
    if filename != Path(filename).name:
        raise TaskArtifactError(
            400,
            "INVALID_IMAGE_FILENAME",
            "图片文件名非法",
            log_detail=f"task_id={record.get('task_id')} well_name={well_name} filename={filename}",
        )
    if is_incomplete_artifact(Path(filename)):
        raise TaskArtifactError(
            409,
            "INCOMPLETE_ARTIFACT_NOT_AVAILABLE",
            "文件仍在写入中，暂不可下载",
            log_detail=f"task_id={record.get('task_id')} well_name={well_name} filename={filename}",
        )
    image_dir = resolve_image_dir(record, well_name)
    file_path = image_dir / filename
    if not file_path.exists() or not file_path.is_file():
        raise TaskArtifactError(
            404,
            "IMAGE_NOT_FOUND",
            "未找到图片",
            log_detail=f"task_id={record.get('task_id')} well_name={well_name} path={file_path}",
        )
    media_type, _ = mimetypes.guess_type(str(file_path))
    return file_path, media_type or "application/octet-stream"
