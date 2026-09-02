"""封装自动对焦执行流程，并支持复用录像相机完成任务前对焦。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from third_party.XWJJJ260511 import run_autofocus
from workflow.config_validator import validate_autofocus_file
from workflow.task_store import task_objective_name


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class RecordingAutofocusCameraAdapter:
    """Adapt the active project camera to third_party autofocus capture()."""

    def __init__(self, cam: Any, work_dir: Path) -> None:
        self.cam = cam
        self.work_dir = work_dir
        self.capture_index = 0
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def capture(self):
        import cv2
        import numpy as np

        self.capture_index += 1
        image_path = self.work_dir / f"autofocus_recording_{self.capture_index:04d}.bmp"
        self.cam.capture_once(str(image_path))
        raw = np.fromfile(str(image_path), dtype=np.uint8)
        frame = cv2.imdecode(raw, cv2.IMREAD_COLOR) if raw.size else None
        if frame is None:
            raise RuntimeError(f"autofocus 读取录像中采样图片失败: {image_path}")
        return frame

    def close(self) -> None:
        return None


def _resolve_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def execute_autofocus_for_task(
    ctx: Dict[str, Any],
    task_cfg: Dict[str, Any],
    objective_result: Dict[str, Any],
    autofocus_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    objective_name = str(task_objective_name(task_cfg) or "").strip()
    if not objective_name:
        raise ValueError("autofocus 需要 task.objective_name 非空")

    config_path_value = autofocus_cfg.get("config_path")
    if not config_path_value:
        raise ValueError("autofocus 启用时，必须提供 autofocus.config_path 或本地 config/autofocus.yaml")

    config_path = _resolve_path(config_path_value)
    if not config_path.exists():
        raise FileNotFoundError(f"未找到 autofocus 配置文件: {config_path}")

    _validate_autofocus_config_path(config_path)

    result = _run_autofocus_reusing_recording_camera(config_path, objective_name)
    if result is None:
        result = run_autofocus(
            str(config_path),
            objective=objective_name,
        )

    focus_log = getattr(result, "focus_log", None) or []
    output_path = getattr(result, "output_path", None)

    return {
        "status": "success",
        "objective_name": objective_name,
        "config_path": str(config_path),
        "best_pos": float(getattr(result, "best_pos")),
        "best_value": float(getattr(result, "best_value")),
        "output_path": str(output_path) if output_path else None,
        "elapsed_sec": float(getattr(result, "elapsed_sec")),
        "focus_log_count": len(focus_log),
        "triggered_by_objective_switch": bool(objective_result.get("switched", False)),
        "reused_recording_camera": bool(getattr(result, "reused_recording_camera", False)),
    }


def _validate_autofocus_config_path(config_path: Path) -> None:
    config_dir = PROJECT_ROOT / "config"
    validate_autofocus_file(
        config_path,
        objectives_path=config_dir / "objectives.yaml",
        camera_path=config_dir / "camera.yaml",
    )


def _run_autofocus_reusing_recording_camera(config_path: Path, objective_name: str):
    from third_party.XWJJJ260511 import run as autofocus_run
    from workflow.camera_executor import close_camera, open_camera

    cfg = autofocus_run._load_yaml_config(config_path)
    motor_cfg = autofocus_run._section(cfg, "motor")
    cfg["motor"] = motor_cfg
    motor_cfg["objective"] = objective_name

    camera_cfg = autofocus_run._section(cfg, "camera")
    camera_settings, _camera_label = autofocus_run._resolve_camera_settings(camera_cfg, motor_cfg)
    backend = str(camera_settings.get("backend", "opencv")).strip().lower()
    if backend != "mvs":
        # OpenCV does not enter the MVS native runtime and keeps the existing
        # third-party implementation.
        return None

    exposure_auto = camera_settings.get("exposure_auto")
    exposure_us = None
    if not bool(exposure_auto):
        exposure_us = camera_settings.get("exposure_time_us", camera_settings.get("exposure_us"))
    camera = open_camera(
        mvs_python_dir=camera_settings.get("mvs_python_dir") or camera_settings.get("mvs_sdk_path"),
        device_index=int(camera_settings.get("device_index", 0)),
        serial_number=camera_settings.get("serial_number"),
        camera_ip=camera_settings.get("ip"),
        pixel_format=str(camera_settings.get("pixel_format") or "mono8"),
        exposure_us=exposure_us,
        exposure_auto=bool(exposure_auto) if exposure_auto is not None else None,
        gain=camera_settings.get("gain"),
    )
    reused_recording_camera = bool(getattr(camera, "recording_shared", False))

    work_dir = PROJECT_ROOT / "data" / "autofocus_recording_tmp"
    adapter = RecordingAutofocusCameraAdapter(camera, work_dir)
    try:
        result = autofocus_run._run_autofocus(adapter, cfg)
        autofocus_run._save_focus_log(
            result.focus_log,
            autofocus_run._get_output_path(cfg, "log_path"),
            cfg,
        )
        setattr(result, "reused_recording_camera", reused_recording_camera)
        return result
    finally:
        close_camera(camera)

