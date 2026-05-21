from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from third_party.XWJJJ260511 import run_autofocus


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
    objective_name = str(task_cfg.get("objective") or "").strip()
    if not objective_name:
        raise ValueError("autofocus 需要 task.objective 非空")

    config_path_value = autofocus_cfg.get("config_path")
    if not config_path_value:
        raise ValueError("autofocus 启用时，必须提供 autofocus.config_path 或本地 config/autofocus.yaml")

    config_path = _resolve_path(config_path_value)
    if not config_path.exists():
        raise FileNotFoundError(f"未找到 autofocus 配置文件: {config_path}")

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


def _run_autofocus_reusing_recording_camera(config_path: Path, objective_name: str):
    from workflow.camera_executor import get_recording_camera

    recording_cam = get_recording_camera()
    if recording_cam is None:
        return None

    from third_party.XWJJJ260511 import run as autofocus_run

    cfg = autofocus_run._load_yaml_config(config_path)
    motor_cfg = autofocus_run._section(cfg, "motor")
    cfg["motor"] = motor_cfg
    motor_cfg["objective"] = objective_name

    work_dir = PROJECT_ROOT / "data" / "autofocus_recording_tmp"
    camera = RecordingAutofocusCameraAdapter(recording_cam, work_dir)
    result = autofocus_run._run_autofocus(camera, cfg)
    autofocus_run._save_focus_log(
        result.focus_log,
        autofocus_run._get_output_path(cfg, "log_path"),
        cfg,
    )
    setattr(result, "reused_recording_camera", True)
    return result

