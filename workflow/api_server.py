from __future__ import annotations

import copy
import logging
import mimetypes
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from workflow.api_errors import (
    API_LOG_PATH,
    access_logger,
    api_error as _api_error,
    configure_api_file_logging as _configure_api_file_logging_base,
    error_detail as _error_detail,
    generic_http_message as _generic_http_message,
    hardware_guard_exception_handler as _hardware_guard_exception_handler,
    http_exception_handler as _http_exception_handler,
    is_public_error_detail as _is_public_error_detail,
    path_guard_exception_handler as _path_guard_exception_handler,
    register_api_error_handlers,
    request_validation_exception_handler as _request_validation_exception_handler,
    task_store_exception_handler as _task_store_exception_handler,
    unhandled_exception_handler as _unhandled_exception_handler,
)
from workflow.api_models import (
    CameraRecordStartRequest,
    ExecuteTaskRequest,
    StageReciprocationStartRequest,
    StageReciprocationStopRequest,
)
from workflow.config_validator import ConfigValidationError, resolve_mvs_python_dir, validate_camera_config, validate_camera_file
from workflow.file_io import read_json_with_retry
from workflow.hardware_guard import (
    STAGE_TERMINAL_STATUSES as _STAGE_TERMINAL_STATUSES,
    acquire_hardware_operation as _acquire_hardware_operation,
    current_hardware_owner as _current_hardware_owner,
    current_hardware_owners as _current_hardware_owners,
    release_hardware_operation as _release_hardware_operation,
)
from workflow.path_guard import (
    CONFIG_ROOT,
    DATA_ROOT,
    OUTPUTS_ROOT,
    PROJECT_ROOT,
    PathGuardError,
    normalize_execute_task_values,
    resolve_config_path as _resolve_config_path,
    resolve_output_path as _resolve_output_path,
    safe_str_path as _safe_str_path,
)
from workflow.run_task import execute_task_request
from workflow.task_store import (
    TASK_ACTIVE_STATUSES as _TASK_ACTIVE_STATUSES,
    TASK_RECORD_IO_LOCK as _TASK_RECORD_IO_LOCK,
    TASK_TERMINAL_STATUSES as _TASK_TERMINAL_STATUSES,
    build_accepted_record as _build_accepted_record,
    build_failed_record as _build_failed_record,
    build_task_record as _build_task_record,
    mark_record_canceled as _mark_record_canceled,
    read_task_record as _read_task_record,
    read_task_record_unlocked as _read_task_record_unlocked,
    recover_interrupted_task_records as _recover_interrupted_task_records,
    sanitize_task_id as _sanitize_task_id,
    task_exists as _task_exists,
    update_task_record as _update_task_record,
    utc_now as _utc_now,
    write_task_record as _write_task_record,
    write_task_record_unlocked as _write_task_record_unlocked,
)
from workflow.task_control import TaskCanceled

IMAGE_SUFFIXES = {".bmp", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}

logger = logging.getLogger(__name__)


def _configure_api_file_logging() -> None:
    _configure_api_file_logging_base(extra_loggers=(logger, access_logger))


_configure_api_file_logging()

_TASK_CANCEL_LOCK = threading.RLock()
_TASK_CANCEL_EVENTS: Dict[str, threading.Event] = {}


@asynccontextmanager
async def _api_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _recover_interrupted_task_records()
    yield


app = FastAPI(title="Colony Workflow API", version="0.3.0", lifespan=_api_lifespan)
register_api_error_handlers(app)


@app.middleware("http")
async def log_request_timing(request, call_next):
    started = time.perf_counter()
    access_logger.info("request start: %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        access_logger.exception(
            "request failed: %s %s elapsed_ms=%.1f",
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    access_logger.info(
        "request end: %s %s status=%s elapsed_ms=%.1f",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


def _load_camera_settings_for_recording(req: CameraRecordStartRequest) -> Dict[str, Any]:
    camera_path = _resolve_config_path(
        req.camera_path or os.getenv("CAMERA_CONFIG_PATH") or str(CONFIG_ROOT / "camera.yaml"),
        "camera_path",
    )
    cfg = validate_camera_file(camera_path)
    camera_cfg = cfg.get("camera", cfg) if isinstance(cfg, dict) else {}
    if not isinstance(camera_cfg, dict):
        camera_cfg = {}
    mvs_python_dir = req.mvs_python_dir if req.mvs_python_dir is not None else resolve_mvs_python_dir(camera_cfg)
    camera_ip = req.ip if req.ip is not None else camera_cfg.get("ip")
    pixel_format = req.pixel_format if req.pixel_format is not None else camera_cfg.get("pixel_format", "mono8")
    if (
        req.mvs_python_dir is not None
        or req.ip is not None
        or req.serial_number is not None
        or req.device_index is not None
        or req.pixel_format is not None
    ):
        effective_camera_cfg = dict(camera_cfg)
        if req.mvs_python_dir is not None:
            effective_camera_cfg["mvs_python_dir"] = req.mvs_python_dir
            effective_camera_cfg.pop("mvs_sdk_path", None)
        if req.ip is not None:
            effective_camera_cfg["ip"] = req.ip
        if req.serial_number is not None:
            effective_camera_cfg["serial_number"] = req.serial_number
        if req.device_index is not None:
            effective_camera_cfg["device_index"] = req.device_index
        if req.pixel_format is not None:
            effective_camera_cfg["pixel_format"] = req.pixel_format
        validate_camera_config({"camera": effective_camera_cfg}, require_top_level=True)
    return {
        "mvs_python_dir": mvs_python_dir,
        "device_index": int(req.device_index if req.device_index is not None else camera_cfg.get("device_index", 0)),
        "serial_number": req.serial_number if req.serial_number is not None else camera_cfg.get("serial_number"),
        "camera_ip": camera_ip,
        "pixel_format": pixel_format,
        "exposure_us": req.exposure_us if req.exposure_us is not None else camera_cfg.get("exposure_us"),
        "gain": req.gain if req.gain is not None else camera_cfg.get("gain"),
        "camera_path": camera_path,
    }


def _normalize_execute_task_request(req: ExecuteTaskRequest) -> ExecuteTaskRequest:
    values = normalize_execute_task_values(
        task=req.task or {},
        camera_path=req.camera_path,
        objectives_path=req.objectives_path,
        plates_path=req.plates_path,
        dump_json=req.dump_json,
    )
    return ExecuteTaskRequest(
        task=values["task"],
        camera_path=values["camera_path"],
        objectives_path=values["objectives_path"],
        plates_path=values["plates_path"],
        dump_json=values["dump_json"],
        persist_result=req.persist_result,
    )


def _register_task_cancel_event(task_id: str, cancel_event: threading.Event) -> None:
    with _TASK_CANCEL_LOCK:
        _TASK_CANCEL_EVENTS[_sanitize_task_id(task_id)] = cancel_event


def _unregister_task_cancel_event(task_id: str) -> None:
    with _TASK_CANCEL_LOCK:
        _TASK_CANCEL_EVENTS.pop(_sanitize_task_id(task_id), None)


def _is_task_cancel_requested(task_id: str) -> bool:
    normalized = _sanitize_task_id(task_id)
    with _TASK_CANCEL_LOCK:
        event = _TASK_CANCEL_EVENTS.get(normalized)
        if event is not None and event.is_set():
            return True
    try:
        record = _read_task_record(normalized)
    except Exception:
        return False
    return bool(record.get("cancel_requested", False))


def _request_task_cancel(task_id: str) -> Dict[str, Any]:
    normalized = _sanitize_task_id(task_id)
    with _TASK_CANCEL_LOCK:
        event = _TASK_CANCEL_EVENTS.get(normalized)
        if event is not None:
            event.set()

    with _TASK_RECORD_IO_LOCK:
        record = _read_task_record_unlocked(normalized)
        if record.get("status") in _TASK_TERMINAL_STATUSES:
            return record
        now = _utc_now()
        record["cancel_requested"] = True
        record["cancel_requested_at"] = now
        record["updated_at"] = now
        record["message"] = "cancel requested; task will stop at the next safe checkpoint"
        _write_task_record_unlocked(record)
        return record


def _ensure_well_record(record: Dict[str, Any], well_name: str) -> Dict[str, Any]:
    wells = record.get("wells") or {}
    if well_name not in wells:
        raise _api_error(
            404,
            "WELL_NOT_FOUND",
            "未找到指定孔位记录",
            log_detail=f"task_id={record.get('task_id')} well_name={well_name}",
        )
    return wells[well_name]


def _resolve_image_dir(record: Dict[str, Any], well_name: str) -> Path:
    well_record = _ensure_well_record(record, well_name)
    image_dir = well_record.get("image_dir")
    if not image_dir:
        raise _api_error(
            404,
            "IMAGE_DIR_NOT_RECORDED",
            "当前孔位未记录图片目录",
            log_detail=f"task_id={record.get('task_id')} well_name={well_name}",
        )
    path = Path(_resolve_output_path(image_dir, f"task.{record.get('task_id')}.wells.{well_name}.image_dir"))
    if not path.exists() or not path.is_dir():
        raise _api_error(
            404,
            "IMAGE_DIR_NOT_FOUND",
            "图片目录不存在",
            log_detail=f"task_id={record.get('task_id')} well_name={well_name} path={path}",
        )
    return path


def _existing_output_path_or_none(value: Any, field_name: str) -> str | None:
    raw = _safe_str_path(value)
    if raw is None:
        return None
    path = Path(_resolve_output_path(raw, field_name))
    return str(path) if path.exists() else None


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
            if record.get("status") in _TASK_TERMINAL_STATUSES:
                return
            if record.get("cancel_requested"):
                return
            patch = _guess_current_progress(record)
            _update_task_record(task_id, patch)
        except Exception:
            logger.exception("monitor task failed: task_id=%s", task_id)
        stop_event.wait(1.0)


def _stop_monitor_thread(monitor: threading.Thread, stop_event: threading.Event, monitor_started: bool) -> None:
    stop_event.set()
    if monitor_started:
        monitor.join(timeout=1.0)


def _run_task_async(task: Dict[str, Any], req: ExecuteTaskRequest, cancel_event: threading.Event) -> None:
    task_id = str(task.get("task_id") or "").strip()
    stop_event = threading.Event()
    monitor = threading.Thread(target=_monitor_running_task, args=(task_id, stop_event), daemon=True)
    monitor_started = False
    try:
        _update_task_record(task_id, {"status": "running", "started_at": _utc_now(), "message": "task started", "progress": 1})
        monitor.start()
        monitor_started = True
        result = execute_task_request(
            raw_task_cfg={"task": task},
            camera_path=req.camera_path or os.getenv("CAMERA_CONFIG_PATH"),
            objectives_path=req.objectives_path or os.getenv("OBJECTIVES_CONFIG_PATH"),
            plates_path=req.plates_path or os.getenv("PLATES_CONFIG_PATH"),
            dump_json=req.dump_json,
            persist_result=req.persist_result,
            cancel_check=lambda: cancel_event.is_set() or _is_task_cancel_requested(task_id),
        )
        _stop_monitor_thread(monitor, stop_event, monitor_started)
        monitor_started = False
        record = _build_task_record(task, result, req.dump_json, req.persist_result)
        _write_task_record(record)
    except TaskCanceled as exc:
        logger.info("task canceled: %s", task_id)
        _stop_monitor_thread(monitor, stop_event, monitor_started)
        monitor_started = False
        try:
            record = _read_task_record(task_id)
        except Exception:
            record = _build_accepted_record(task, req.dump_json, req.persist_result)
        _write_task_record(_mark_record_canceled(record, str(exc)))
    except Exception as exc:
        logger.exception("task execution failed: %s", task_id)
        _stop_monitor_thread(monitor, stop_event, monitor_started)
        monitor_started = False
        record = _build_failed_record(
            task,
            "任务执行失败，请查看本地日志或联系维护人员",
            req.dump_json,
            req.persist_result,
        )
        _write_task_record(record)
    finally:
        _stop_monitor_thread(monitor, stop_event, monitor_started)
        _unregister_task_cancel_event(task_id)
        _release_hardware_operation("task", task_id)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/camera/record/status")
def get_camera_record_status() -> Dict[str, Any]:
    from workflow.camera_executor import recording_camera_status

    return recording_camera_status()


@app.get("/api/hardware/status")
def get_hardware_status() -> Dict[str, Any]:
    owners = _current_hardware_owners()
    return {
        "busy": bool(owners),
        "owners": owners,
        "owner": owners[0] if owners else {},
    }


@app.post("/api/camera/record/start")
def start_camera_record(req: CameraRecordStartRequest) -> Dict[str, Any]:
    from workflow.camera_executor import start_recording_camera

    try:
        settings = _load_camera_settings_for_recording(req)
        save_path = _resolve_output_path(req.save_path, "save_path")
        if save_path is None:
            raise _api_error(400, "CAMERA_RECORD_SAVE_PATH_REQUIRED", "录像保存路径不能为空")
    except (HTTPException, PathGuardError):
        raise
    except (ConfigValidationError, ValueError, OSError) as exc:
        raise _api_error(
            400,
            "CAMERA_RECORD_CONFIG_INVALID",
            "相机录像配置无效，请检查 camera.yaml 或请求参数",
            log_detail=str(exc),
        ) from exc
    except Exception as exc:
        raise _api_error(
            400,
            "CAMERA_RECORD_CONFIG_LOAD_FAILED",
            "相机录像配置加载失败",
            log_detail=str(exc),
            exc=exc,
        ) from exc

    operation_id = str(save_path)
    _acquire_hardware_operation("camera_record", operation_id)
    try:
        result = start_recording_camera(
            save_path=str(save_path),
            mvs_python_dir=settings.get("mvs_python_dir"),
            device_index=int(settings.get("device_index", 0)),
            serial_number=settings.get("serial_number"),
            camera_ip=settings.get("camera_ip"),
            pixel_format=settings.get("pixel_format", "mono8"),
            exposure_us=settings.get("exposure_us"),
            gain=settings.get("gain"),
            fps=req.fps,
            bitrate_kbps=int(req.bitrate_kbps),
            timeout_ms=req.timeout_ms,
        )
        result["camera_path"] = settings.get("camera_path")
        return result
    except Exception as exc:
        _release_hardware_operation("camera_record", operation_id)
        raise _api_error(
            409,
            "CAMERA_RECORD_START_FAILED",
            "相机录像启动失败，请检查相机连接或硬件状态",
            log_detail=f"save_path={save_path} error={exc}",
            exc=exc,
        ) from exc


@app.post("/api/camera/record/stop")
def stop_camera_record() -> Dict[str, Any]:
    from workflow.camera_executor import stop_recording_camera

    try:
        result = stop_recording_camera()
        camera_owner = None
        for owner in _current_hardware_owners():
            if owner.get("kind") == "camera_record":
                camera_owner = owner
                break
        if camera_owner is not None:
            _release_hardware_operation("camera_record", str(camera_owner.get("operation_id") or ""))
        return result
    except Exception as exc:
        raise _api_error(
            409,
            "CAMERA_RECORD_STOP_FAILED",
            "相机录像停止失败，请检查硬件状态",
            log_detail=str(exc),
            exc=exc,
        ) from exc


@app.post("/api/stage/reciprocation/start", status_code=202)
def start_stage_reciprocation(req: StageReciprocationStartRequest | None = None) -> Dict[str, Any]:
    from workflow.stage_reciprocation import stage_reciprocation_controller

    req = req or StageReciprocationStartRequest()
    cfg = req.model_dump()
    operation_id = "stage_reciprocation"
    _acquire_hardware_operation("stage_reciprocation", operation_id)
    try:
        return stage_reciprocation_controller.start(cfg)
    except Exception as exc:
        _release_hardware_operation("stage_reciprocation", operation_id)
        raise _api_error(
            409,
            "STAGE_RECIPROCATION_START_FAILED",
            "位移台往复运动启动失败，请检查位移台连接或参数",
            log_detail=str(exc),
            exc=exc,
        ) from exc


@app.post("/api/stage/reciprocation/stop")
def stop_stage_reciprocation(req: StageReciprocationStopRequest | None = None) -> Dict[str, Any]:
    from workflow.stage_reciprocation import stage_reciprocation_controller

    try:
        join_timeout_s = 5.0 if req is None else req.join_timeout_s
        result = stage_reciprocation_controller.stop(join_timeout_s=join_timeout_s)
        if result.get("status") in _STAGE_TERMINAL_STATUSES:
            _release_hardware_operation("stage_reciprocation", "stage_reciprocation")
        return result
    except Exception as exc:
        raise _api_error(
            409,
            "STAGE_RECIPROCATION_STOP_FAILED",
            "位移台往复运动停止失败，请检查位移台状态",
            log_detail=str(exc),
            exc=exc,
        ) from exc


@app.get("/api/stage/reciprocation/status")
def get_stage_reciprocation_status() -> Dict[str, Any]:
    from workflow.stage_reciprocation import stage_reciprocation_controller

    return stage_reciprocation_controller.status()


@app.post("/api/tasks/execute", status_code=202)
def execute_task(req: ExecuteTaskRequest) -> Dict[str, Any]:
    req = _normalize_execute_task_request(req)
    task = req.task or {}
    task_id = str(task.get("task_id") or "").strip()
    access_logger.info("execute_task entered: task_id=%s", task_id or "<empty>")
    if not task_id:
        raise _api_error(400, "TASK_ID_REQUIRED", "任务 ID 不能为空")

    if _task_exists(task_id):
        old = _read_task_record(task_id)
        if old.get("status") in _TASK_ACTIVE_STATUSES:
            raise _api_error(
                409,
                "TASK_ALREADY_RUNNING",
                "任务正在执行中，请勿重复提交",
                log_detail=f"task_id={task_id}",
            )

    _acquire_hardware_operation("task", task_id)
    cancel_event = threading.Event()
    _register_task_cancel_event(task_id, cancel_event)
    try:
        record = _build_accepted_record(task, req.dump_json, req.persist_result)
        access_logger.info("execute_task writing accepted record: task_id=%s", task_id)
        _write_task_record(record)

        worker = threading.Thread(target=_run_task_async, args=(copy.deepcopy(task), req, cancel_event), daemon=True)
        worker.start()
    except Exception:
        _unregister_task_cancel_event(task_id)
        _release_hardware_operation("task", task_id)
        raise
    access_logger.info("execute_task worker started: task_id=%s thread=%s", task_id, worker.name)

    return {
        "task_id": task_id,
        "status": "accepted",
        "task_type": task.get("task_type"),
        "observe_scope": task.get("observe_scope"),
        "message": "task accepted",
        "result_json_path": record.get("result_json_path"),
    }


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> Dict[str, Any]:
    record = _request_task_cancel(task_id)
    status = record.get("status")
    if status in _TASK_TERMINAL_STATUSES:
        return {
            "task_id": record.get("task_id"),
            "status": status,
            "cancel_requested": bool(record.get("cancel_requested", False)),
            "message": "task is already terminal",
        }

    return {
        "task_id": record.get("task_id"),
        "status": "cancel_requested",
        "previous_status": status,
        "cancel_requested": True,
        "cancel_requested_at": record.get("cancel_requested_at"),
        "message": record.get("message"),
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
        "error_code": record.get("error_code"),
        "error": record.get("error"),
        "cancel_requested": record.get("cancel_requested", False),
        "cancel_requested_at": record.get("cancel_requested_at"),
        "canceled_at": record.get("canceled_at"),
        "cancel_reason": record.get("cancel_reason"),
    }


@app.get("/api/tasks/{task_id}/result")
def get_task_result(task_id: str) -> Dict[str, Any]:
    record = _read_task_record(task_id)
    if record.get("status") in _TASK_ACTIVE_STATUSES:
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
        p = Path(_resolve_output_path(result_json_path, f"task.{task_id}.result_json_path"))
        if p.exists() and p.is_file():
            try:
                result = read_json_with_retry(p)
            except Exception as exc:
                raise _api_error(
                    503,
                    "RESULT_JSON_TEMPORARILY_UNREADABLE",
                    "结果文件暂时不可读，请稍后重试",
                    log_detail=f"task_id={task_id} path={p}",
                    exc=exc,
                ) from exc
            if not isinstance(result, dict):
                raise _api_error(
                    500,
                    "RESULT_JSON_INVALID",
                    "结果文件格式异常，请查看本地日志",
                    log_detail=f"task_id={task_id} path={p}",
                )
            return result
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
        "capture_result_json": _existing_output_path_or_none(capture_path, f"task.{task_id}.{well_name}.capture_result_json"),
        "detect_result_json": _existing_output_path_or_none(detect_path, f"task.{task_id}.{well_name}.detect_result_json"),
        "compensate_result_json": _existing_output_path_or_none(compensate_path, f"task.{task_id}.{well_name}.compensate_result_json"),
        "images": images,
    }


@app.get("/api/tasks/{task_id}/wells/{well_name}/images/{filename}")
def download_well_image(task_id: str, well_name: str, filename: str):
    if filename != Path(filename).name:
        raise _api_error(
            400,
            "INVALID_IMAGE_FILENAME",
            "图片文件名非法",
            log_detail=f"task_id={task_id} well_name={well_name} filename={filename}",
        )
    record = _read_task_record(task_id)
    image_dir = _resolve_image_dir(record, well_name)
    file_path = image_dir / filename
    if not file_path.exists() or not file_path.is_file():
        raise _api_error(
            404,
            "IMAGE_NOT_FOUND",
            "未找到图片",
            log_detail=f"task_id={task_id} well_name={well_name} path={file_path}",
        )
    media_type, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(
        path=file_path,
        media_type=media_type or "application/octet-stream",
        filename=file_path.name,
    )


