from __future__ import annotations

import json
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from workflow.run_task import execute_task_request

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TASK_INDEX_DIR = PROJECT_ROOT / "data" / "task_index"

app = FastAPI(title="Colony Workflow API", version="0.2.0")


class ExecuteTaskRequest(BaseModel):
    task: Dict[str, Any]
    camera_path: str | None = Field(default=None, description="可选，覆盖默认 camera.yaml")
    objectives_path: str | None = Field(default=None, description="可选，覆盖默认 objectives.yaml")
    plates_path: str | None = Field(default=None, description="可选，覆盖默认 plates.yaml")
    dump_json: str | None = Field(default=None, description="可选，覆盖结果落盘路径")
    persist_result: bool = Field(default=True, description="是否仍然把结果写到本地文件")


def _task_index_dir() -> Path:
    raw = os.getenv("TASK_INDEX_DIR")
    return Path(raw) if raw else DEFAULT_TASK_INDEX_DIR


def _sanitize_task_id(task_id: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(task_id).strip())
    if not s:
        raise ValueError("非法 task_id")
    return s


def _task_record_path(task_id: str) -> Path:
    task_id = _sanitize_task_id(task_id)
    index_dir = _task_index_dir()
    index_dir.mkdir(parents=True, exist_ok=True)
    return index_dir / f"{task_id}.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_str_path(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


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


def _build_well_artifacts_from_result(result: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
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
            capture_result_json = _safe_str_path(item.get("capture_result_json"))
            detect_result_json = _safe_str_path(item.get("detect_result_json"))
            compensate_result_json = _safe_str_path(item.get("compensate_result_json"))
            image_dir = None
            if capture_result_json:
                image_dir = str(Path(capture_result_json).parent / "images")
            wells_info[well_name] = {
                "image_dir": image_dir,
                "capture_result_json": capture_result_json,
                "detect_result_json": detect_result_json,
                "compensate_result_json": compensate_result_json,
            }
        return wells_info

    well_name = str(result.get("well_name") or task.get("well_name") or (task.get("target", {}) or {}).get("well_name") or "").strip()
    if not well_name:
        return {}

    image_dir = None
    capture_result_json = None
    detect_result_json = _safe_str_path(task_detect.get("output_json")) or _safe_str_path(task_output.get("detect_json"))
    compensate_result_json = _safe_str_path(task_comp.get("output_json")) or _safe_str_path(task_output.get("compensate_json"))

    if result.get("capture_result"):
        image_dir = _first_saved_image_dir(result.get("capture_result"))

    if image_dir is None:
        save_dir = _safe_str_path(task_capture.get("save_dir"))
        if save_dir:
            image_dir = save_dir

    return {
        well_name: {
            "image_dir": image_dir,
            "capture_result_json": capture_result_json,
            "detect_result_json": detect_result_json,
            "compensate_result_json": compensate_result_json,
        }
    }


def _build_task_record(task: Dict[str, Any], result: Dict[str, Any], dump_json: str | None, persist_result: bool) -> Dict[str, Any]:
    task_id = str(result.get("task_id") or task.get("task_id") or "")
    output_cfg = task.get("output", {}) or {}
    result_json_path = _safe_str_path(dump_json) or _safe_str_path(output_cfg.get("result_json"))

    return {
        "task_id": task_id,
        "status": result.get("status"),
        "task_type": result.get("task_type"),
        "observe_scope": result.get("observe_scope"),
        "plate_type": result.get("plate_type"),
        "objective_name": result.get("objective_name"),
        "stored_at_utc": _utc_now(),
        "persist_result": bool(persist_result),
        "result_json_path": result_json_path,
        "base_save_dir": _safe_str_path(result.get("base_save_dir")),
        "wells": _build_well_artifacts_from_result(result, task),
        "result": result,
    }


def _build_failed_record(task: Dict[str, Any], error: str, dump_json: str | None, persist_result: bool) -> Dict[str, Any]:
    task_id = str(task.get("task_id") or "")
    output_cfg = task.get("output", {}) or {}
    observe_scope = str(task.get("observe_scope") or "").lower()
    wells = {}
    if observe_scope == "well_list":
        target = task.get("target", {}) or {}
        for w in target.get("well_list", []) or []:
            wells[str(w)] = {"image_dir": None, "capture_result_json": None, "detect_result_json": None, "compensate_result_json": None}
    elif observe_scope == "single_well":
        well_name = str(task.get("well_name") or (task.get("target", {}) or {}).get("well_name") or "")
        if well_name:
            wells[well_name] = {"image_dir": None, "capture_result_json": None, "detect_result_json": None, "compensate_result_json": None}

    return {
        "task_id": task_id,
        "status": "failed",
        "task_type": task.get("task_type"),
        "observe_scope": task.get("observe_scope"),
        "plate_type": task.get("plate_type"),
        "objective_name": task.get("objective"),
        "stored_at_utc": _utc_now(),
        "persist_result": bool(persist_result),
        "result_json_path": _safe_str_path(dump_json) or _safe_str_path(output_cfg.get("result_json")),
        "base_save_dir": None,
        "wells": wells,
        "error": error,
        "request_task": task,
    }


def _write_task_record(record: Dict[str, Any]) -> None:
    path = _task_record_path(record["task_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_task_record(task_id: str) -> Dict[str, Any]:
    path = _task_record_path(task_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"未找到任务记录: {task_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _ensure_well_record(record: Dict[str, Any], well_name: str) -> Dict[str, Any]:
    wells = record.get("wells") or {}
    if well_name not in wells:
        raise HTTPException(status_code=404, detail=f"任务 {record.get('task_id')} 中未找到孔位 {well_name}")
    return wells[well_name]


def _resolve_image_dir(record: Dict[str, Any], well_name: str) -> Path:
    well_record = _ensure_well_record(record, well_name)
    image_dir = well_record.get("image_dir")
    if not image_dir:
        raise HTTPException(status_code=404, detail=f"任务 {record.get('task_id')} 的孔位 {well_name} 未记录图片目录")
    path = Path(image_dir)
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=404, detail=f"图片目录不存在: {path}")
    return path


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/tasks/execute")
def execute_task(req: ExecuteTaskRequest) -> Dict[str, Any]:
    task = req.task or {}
    task_id = str(task.get("task_id") or "").strip()
    try:
        result = execute_task_request(
            raw_task_cfg={"task": task},
            camera_path=req.camera_path or os.getenv("CAMERA_CONFIG_PATH"),
            objectives_path=req.objectives_path or os.getenv("OBJECTIVES_CONFIG_PATH"),
            plates_path=req.plates_path or os.getenv("PLATES_CONFIG_PATH"),
            dump_json=req.dump_json,
            persist_result=req.persist_result,
        )
        record = _build_task_record(task, result, req.dump_json, req.persist_result)
        _write_task_record(record)
        return result
    except Exception as exc:
        if task_id:
            record = _build_failed_record(task, str(exc), req.dump_json, req.persist_result)
            _write_task_record(record)
            raise HTTPException(status_code=500, detail=record)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/tasks/{task_id}/status")
def get_task_status(task_id: str) -> Dict[str, Any]:
    record = _read_task_record(task_id)
    return {
        "task_id": record.get("task_id"),
        "status": record.get("status"),
        "task_type": record.get("task_type"),
        "observe_scope": record.get("observe_scope"),
        "stored_at_utc": record.get("stored_at_utc"),
        "result_json_path": record.get("result_json_path"),
    }


@app.get("/api/tasks/{task_id}/result")
def get_task_result(task_id: str) -> Dict[str, Any]:
    record = _read_task_record(task_id)
    result_json_path = record.get("result_json_path")
    if result_json_path:
        p = Path(result_json_path)
        if p.exists() and p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    result = record.get("result")
    if result is not None:
        return result
    return record


@app.get("/api/tasks/{task_id}/wells/{well_name}/images")
def list_well_images(task_id: str, well_name: str) -> Dict[str, Any]:
    record = _read_task_record(task_id)
    image_dir = _resolve_image_dir(record, well_name)
    images = sorted([p.name for p in image_dir.iterdir() if p.is_file()])
    well_record = _ensure_well_record(record, well_name)
    return {
        "task_id": record.get("task_id"),
        "well_name": well_name,
        "image_dir": str(image_dir),
        "capture_result_json": well_record.get("capture_result_json"),
        "detect_result_json": well_record.get("detect_result_json"),
        "compensate_result_json": well_record.get("compensate_result_json"),
        "images": images,
    }


@app.get("/api/tasks/{task_id}/wells/{well_name}/images/{filename}")
def download_well_image(task_id: str, well_name: str, filename: str):
    if filename != Path(filename).name:
        raise HTTPException(status_code=400, detail="非法文件名")
    record = _read_task_record(task_id)
    image_dir = _resolve_image_dir(record, well_name)
    file_path = image_dir / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"未找到图片: {filename}")
    media_type, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(
        path=file_path,
        media_type=media_type or "application/octet-stream",
        filename=file_path.name,
    )

