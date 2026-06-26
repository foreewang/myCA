from __future__ import annotations

import os
from typing import Any, Callable, Dict

from workflow.api_models import CameraRecordStartRequest
from workflow.config_validator import ConfigValidationError, resolve_mvs_python_dir, validate_camera_config, validate_camera_file
from workflow.hardware_guard import (
    acquire_hardware_operation,
    current_hardware_owners,
    release_hardware_operation,
)
from workflow.path_guard import CONFIG_ROOT, resolve_config_path, resolve_output_path


class CameraRecordServiceError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        *,
        log_detail: str | None = None,
        cause: BaseException | None = None,
        log_exception: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.error_code = str(error_code)
        self.message = str(message)
        self.log_detail = log_detail
        self.cause = cause
        self.log_exception = bool(log_exception)


def load_camera_settings_for_recording(req: CameraRecordStartRequest) -> Dict[str, Any]:
    camera_path = resolve_config_path(
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


def _config_error_from_exception(exc: BaseException) -> CameraRecordServiceError:
    if isinstance(exc, (ConfigValidationError, ValueError, OSError)):
        return CameraRecordServiceError(
            400,
            "CAMERA_RECORD_CONFIG_INVALID",
            "相机录像配置无效，请检查 camera.yaml 或请求参数",
            log_detail=str(exc),
            cause=exc,
            log_exception=False,
        )
    return CameraRecordServiceError(
        400,
        "CAMERA_RECORD_CONFIG_LOAD_FAILED",
        "相机录像配置加载失败",
        log_detail=str(exc),
        cause=exc,
        log_exception=True,
    )


def start_camera_recording(
    req: CameraRecordStartRequest,
    *,
    settings_loader: Callable[[CameraRecordStartRequest], Dict[str, Any]] = load_camera_settings_for_recording,
) -> Dict[str, Any]:
    from workflow.camera_executor import start_recording_camera

    try:
        settings = settings_loader(req)
        save_path = resolve_output_path(req.save_path, "save_path")
        if save_path is None:
            raise CameraRecordServiceError(
                400,
                "CAMERA_RECORD_SAVE_PATH_REQUIRED",
                "录像保存路径不能为空",
            )
    except CameraRecordServiceError:
        raise
    except Exception as exc:
        raise _config_error_from_exception(exc) from exc

    operation_id = str(save_path)
    acquire_hardware_operation("camera_record", operation_id)
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
        release_hardware_operation("camera_record", operation_id)
        raise CameraRecordServiceError(
            409,
            "CAMERA_RECORD_START_FAILED",
            "相机录像启动失败，请检查相机连接或硬件状态",
            log_detail=f"save_path={save_path} error={exc}",
            cause=exc,
            log_exception=True,
        ) from exc


def stop_camera_recording() -> Dict[str, Any]:
    from workflow.camera_executor import stop_recording_camera

    try:
        result = stop_recording_camera()
        camera_owner = None
        for owner in current_hardware_owners():
            if owner.get("kind") == "camera_record":
                camera_owner = owner
                break
        if camera_owner is not None:
            release_hardware_operation("camera_record", str(camera_owner.get("operation_id") or ""))
        return result
    except Exception as exc:
        raise CameraRecordServiceError(
            409,
            "CAMERA_RECORD_STOP_FAILED",
            "相机录像停止失败，请检查硬件状态",
            log_detail=str(exc),
            cause=exc,
            log_exception=True,
        ) from exc
