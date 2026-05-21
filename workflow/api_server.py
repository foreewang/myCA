from __future__ import annotations

import copy
import json
import logging
import mimetypes
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from workflow.run_task import execute_task_request, load_structured_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TASK_INDEX_DIR = PROJECT_ROOT / "data" / "task_index"
IMAGE_SUFFIXES = {".bmp", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}

app = FastAPI(title="Colony Workflow API", version="0.3.0")
logger = logging.getLogger(__name__)


class ExecuteTaskRequest(BaseModel):
    task: Dict[str, Any]
    camera_path: str | None = Field(default=None, description="可选，覆盖默认 camera.yaml")
    objectives_path: str | None = Field(default=None, description="可选，覆盖默认 objectives.yaml")
    plates_path: str | None = Field(default=None, description="可选，覆盖默认 plates.yaml")
    dump_json: str | None = Field(default=None, description="可选，覆盖结果落盘路径")
    persist_result: bool = Field(default=True, description="是否仍然把结果写到本地文件")


class CameraRecordStartRequest(BaseModel):
    save_path: str = Field(default="data/camera_records/recording.avi")
    camera_path: str | None = None
    device_index: int | None = None
    serial_number: str | None = None
    mvs_python_dir: str | None = None
    exposure_us: float | None = None
    gain: float | None = None
    fps: float | None = Field(default=10.0)
    bitrate_kbps: int = Field(default=1000)
    timeout_ms: int | None = None


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


def _load_camera_settings_for_recording(req: CameraRecordStartRequest) -> Dict[str, Any]:
    camera_path = req.camera_path or os.getenv("CAMERA_CONFIG_PATH") or str(PROJECT_ROOT / "config" / "camera.yaml")
    cfg = load_structured_file(camera_path)
    camera_cfg = cfg.get("camera", cfg) if isinstance(cfg, dict) else {}
    if not isinstance(camera_cfg, dict):
        camera_cfg = {}
    return {
        "mvs_python_dir": req.mvs_python_dir if req.mvs_python_dir is not None else camera_cfg.get("mvs_sdk_path") or camera_cfg.get("mvs_python_dir"),
        "device_index": int(req.device_index if req.device_index is not None else camera_cfg.get("device_index", 0)),
        "serial_number": req.serial_number if req.serial_number is not None else camera_cfg.get("serial_number"),
        "exposure_us": req.exposure_us if req.exposure_us is not None else camera_cfg.get("exposure_us"),
        "gain": req.gain if req.gain is not None else camera_cfg.get("gain"),
        "camera_path": camera_path,
    }


def _safe_str_path(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _write_task_record(record: Dict[str, Any]) -> None:
    path = _task_record_path(record["task_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_task_record(task_id: str) -> Dict[str, Any]:
    path = _task_record_path(task_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"未找到任务记录: {task_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _task_exists(task_id: str) -> bool:
    return _task_record_path(task_id).exists()


def _update_task_record(task_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    record = _read_task_record(task_id)
    record.update(patch)
    record["updated_at"] = _utc_now()
    _write_task_record(record)
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


def _guess_well_artifacts_from_task(task: Dict[str, Any]) -> Dict[str, Any]:
    observe_scope = str(task.get("observe_scope") or "").lower()
    capture_cfg = task.get("capture", {}) or {}
    detect_cfg = task.get("detect", {}) or {}
    output_cfg = task.get("output", {}) or {}
    comp_cfg = task.get("compensate", {}) or {}
    save_dir = _safe_str_path(capture_cfg.get("save_dir"))
    detect_output_json = _safe_str_path(detect_cfg.get("output_json")) or _safe_str_path(output_cfg.get("detect_json"))
    compensate_output_json = _safe_str_path(comp_cfg.get("output_json")) or _safe_str_path(output_cfg.get("compensate_json"))

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
            "status": "success",
            "progress": 100,
            "message": "completed",
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
        "created_at": _utc_now(),
        "started_at": None,
        "updated_at": _utc_now(),
        "finished_at": _utc_now(),
        "persist_result": bool(persist_result),
        "result_json_path": result_json_path,
        "base_save_dir": _safe_str_path(result.get("base_save_dir")),
        "progress": 100,
        "message": "task completed",
        "current_stage": None,
        "current_well": None,
        "wells": _build_well_artifacts_from_result(result, task),
        "result": result,
        "request_task": task,
    }


def _build_failed_record(task: Dict[str, Any], error: str, dump_json: str | None, persist_result: bool) -> Dict[str, Any]:
    task_id = str(task.get("task_id") or "")
    output_cfg = task.get("output", {}) or {}
    return {
        "task_id": task_id,
        "status": "failed",
        "task_type": task.get("task_type"),
        "observe_scope": task.get("observe_scope"),
        "plate_type": task.get("plate_type"),
        "objective_name": task.get("objective"),
        "stored_at_utc": _utc_now(),
        "created_at": _utc_now(),
        "started_at": None,
        "updated_at": _utc_now(),
        "finished_at": _utc_now(),
        "persist_result": bool(persist_result),
        "result_json_path": _safe_str_path(dump_json) or _safe_str_path(output_cfg.get("result_json")),
        "base_save_dir": None,
        "progress": 100,
        "message": error,
        "current_stage": None,
        "current_well": None,
        "wells": _guess_well_artifacts_from_task(task),
        "error": error,
        "request_task": task,
    }


def _build_accepted_record(task: Dict[str, Any], dump_json: str | None, persist_result: bool) -> Dict[str, Any]:
    output_cfg = task.get("output", {}) or {}
    return {
        "task_id": str(task.get("task_id") or ""),
        "status": "queued",
        "task_type": task.get("task_type"),
        "observe_scope": task.get("observe_scope"),
        "plate_type": task.get("plate_type"),
        "objective_name": task.get("objective"),
        "stored_at_utc": None,
        "created_at": _utc_now(),
        "started_at": None,
        "updated_at": _utc_now(),
        "finished_at": None,
        "persist_result": bool(persist_result),
        "result_json_path": _safe_str_path(dump_json) or _safe_str_path(output_cfg.get("result_json")),
        "base_save_dir": _safe_str_path((task.get("capture", {}) or {}).get("save_dir")),
        "progress": 0,
        "message": "task accepted",
        "current_stage": None,
        "current_well": None,
        "wells": _guess_well_artifacts_from_task(task),
        "result": None,
        "request_task": task,
    }


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


def _count_images(image_dir: Path) -> int:
    if not image_dir.exists() or not image_dir.is_dir():
        return 0
    return sum(1 for p in image_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


def _guess_current_progress(record: Dict[str, Any]) -> Dict[str, Any]:
    task_type = str(record.get("task_type") or "").lower()
    stage = "capture"
    message = "running"
    progress = max(int(record.get("progress") or 0), 1)
    current_well = record.get("current_well")

    wells = record.get("wells") or {}
    if wells:
        total_imgs = 0
        first_active = None
        detect_done = 0
        for well_name, meta in wells.items():
            image_dir = Path(meta["image_dir"]) if meta.get("image_dir") else None
            img_count = _count_images(image_dir) if image_dir else 0
            total_imgs += img_count
            if img_count > 0 and first_active is None:
                first_active = well_name
            detect_json = meta.get("detect_result_json")
            if detect_json and Path(detect_json).exists():
                detect_done += 1

        current_well = first_active or current_well
        if task_type == "pipeline":
            if detect_done > 0:
                stage = "detect"
                progress = max(progress, 80 if detect_done < len(wells) else 95)
                message = f"detecting, wells done={detect_done}/{len(wells)}"
            else:
                stage = "capture"
                progress = max(progress, 10 if total_imgs == 0 else min(70, 10 + total_imgs))
                message = f"capturing, images saved={total_imgs}"
        elif task_type == "capture":
            stage = "capture"
            progress = max(progress, 10 if total_imgs == 0 else min(95, 10 + total_imgs))
            message = f"capturing, images saved={total_imgs}"
        elif task_type == "compensate":
            stage = "compensate"
            progress = max(progress, 50)
            message = "compensating"
    else:
        if task_type == "compensate":
            stage = "compensate"
            progress = max(progress, 50)
            message = "compensating"

    return {
        "status": "running",
        "progress": progress,
        "message": message,
        "current_stage": stage,
        "current_well": current_well,
    }


def _monitor_running_task(task_id: str, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            record = _read_task_record(task_id)
            if record.get("status") in {"success", "failed"}:
                return
            patch = _guess_current_progress(record)
            _update_task_record(task_id, patch)
        except Exception:
            logger.exception("monitor task failed: task_id=%s", task_id)
        stop_event.wait(1.0)


def _run_task_async(task: Dict[str, Any], req: ExecuteTaskRequest) -> None:
    task_id = str(task.get("task_id") or "").strip()
    stop_event = threading.Event()
    monitor = threading.Thread(target=_monitor_running_task, args=(task_id, stop_event), daemon=True)
    try:
        _update_task_record(task_id, {"status": "running", "started_at": _utc_now(), "message": "task started", "progress": 1})
        monitor.start()
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
    except Exception as exc:
        logger.exception("task execution failed: %s", task_id)
        record = _build_failed_record(task, str(exc), req.dump_json, req.persist_result)
        _write_task_record(record)
    finally:
        stop_event.set()
        monitor.join(timeout=1.0)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/camera/record/status")
def get_camera_record_status() -> Dict[str, Any]:
    from workflow.camera_executor import recording_camera_status

    return recording_camera_status()


@app.post("/api/camera/record/start")
def start_camera_record(req: CameraRecordStartRequest) -> Dict[str, Any]:
    from workflow.camera_executor import start_recording_camera

    settings = _load_camera_settings_for_recording(req)
    try:
        result = start_recording_camera(
            save_path=req.save_path,
            mvs_python_dir=settings.get("mvs_python_dir"),
            device_index=int(settings.get("device_index", 0)),
            serial_number=settings.get("serial_number"),
            exposure_us=settings.get("exposure_us"),
            gain=settings.get("gain"),
            fps=req.fps,
            bitrate_kbps=int(req.bitrate_kbps),
            timeout_ms=req.timeout_ms,
        )
        result["camera_path"] = settings.get("camera_path")
        return result
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/camera/record/stop")
def stop_camera_record() -> Dict[str, Any]:
    from workflow.camera_executor import stop_recording_camera

    try:
        return stop_recording_camera()
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/tasks/execute", status_code=202)
def execute_task(req: ExecuteTaskRequest) -> Dict[str, Any]:
    task = req.task or {}
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise HTTPException(status_code=400, detail="task.task_id 不能为空")

    if _task_exists(task_id):
        old = _read_task_record(task_id)
        if old.get("status") in {"queued", "running"}:
            raise HTTPException(status_code=409, detail=f"任务正在执行中: {task_id}")

    record = _build_accepted_record(task, req.dump_json, req.persist_result)
    _write_task_record(record)

    worker = threading.Thread(target=_run_task_async, args=(copy.deepcopy(task), req), daemon=True)
    worker.start()

    return {
        "task_id": task_id,
        "status": "accepted",
        "task_type": task.get("task_type"),
        "observe_scope": task.get("observe_scope"),
        "message": "task accepted",
        "result_json_path": record.get("result_json_path"),
    }


@app.get("/api/tasks/{task_id}/status")
def get_task_status(task_id: str) -> Dict[str, Any]:
    record = _read_task_record(task_id)
    return {
        "task_id": record.get("task_id"),
        "status": record.get("status"),
        "task_type": record.get("task_type"),
        "observe_scope": record.get("observe_scope"),
        "plate_type": record.get("plate_type"),
        "objective_name": record.get("objective_name"),
        "progress": record.get("progress", 0),
        "message": record.get("message"),
        "current_stage": record.get("current_stage"),
        "current_well": record.get("current_well"),
        "created_at": record.get("created_at"),
        "started_at": record.get("started_at"),
        "updated_at": record.get("updated_at"),
        "finished_at": record.get("finished_at"),
        "stored_at_utc": record.get("stored_at_utc"),
        "result_json_path": record.get("result_json_path"),
        "error": record.get("error"),
    }


@app.get("/api/tasks/{task_id}/result")
def get_task_result(task_id: str) -> Dict[str, Any]:
    record = _read_task_record(task_id)
    if record.get("status") in {"queued", "running"}:
        return {
            "task_id": record.get("task_id"),
            "status": record.get("status"),
            "progress": record.get("progress", 0),
            "message": record.get("message"),
            "current_stage": record.get("current_stage"),
            "current_well": record.get("current_well"),
            "result_json_path": record.get("result_json_path"),
            "result": None,
        }

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
    images = sorted([p.name for p in image_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES])
    well_record = _ensure_well_record(record, well_name)
    capture_path = well_record.get("capture_result_json")
    detect_path = well_record.get("detect_result_json")
    compensate_path = well_record.get("compensate_result_json")
    return {
        "task_id": record.get("task_id"),
        "well_name": well_name,
        "image_dir": str(image_dir),
        "capture_result_json": capture_path if capture_path and Path(capture_path).exists() else None,
        "detect_result_json": detect_path if detect_path and Path(detect_path).exists() else None,
        "compensate_result_json": compensate_path if compensate_path and Path(compensate_path).exists() else None,
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


