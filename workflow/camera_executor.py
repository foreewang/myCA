from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEVICES_DIR = PROJECT_ROOT / "devices"

if str(DEVICES_DIR) not in sys.path:
    sys.path.insert(0, str(DEVICES_DIR))

from camera_controller import HikCameraController  # type: ignore


def build_image_name(pattern: str, format_kwargs: Dict[str, Any]) -> str:
    try:
        return pattern.format(**format_kwargs)
    except KeyError as exc:
        raise KeyError(
            f"文件名模板缺少字段 {exc}。当前可用字段: {sorted(format_kwargs.keys())}"
        ) from exc


def _safe_int_attr(obj: Any, name: str, default: int | None = None) -> int | None:
    value = getattr(obj, name, None)
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def frameinfo_to_dict(frame: Any, saved_path: str) -> Dict[str, Any]:
    if frame is None:
        return {
            "saved_path": saved_path,
            "width": None,
            "height": None,
            "frame_num": None,
            "pixel_type": None,
            "frame_len": None,
        }

    return {
        "saved_path": saved_path,
        "width": _safe_int_attr(frame, "width", _safe_int_attr(frame, "nWidth")),
        "height": _safe_int_attr(frame, "height", _safe_int_attr(frame, "nHeight")),
        "frame_num": _safe_int_attr(frame, "frame_num", _safe_int_attr(frame, "nFrameNum")),
        "pixel_type": _safe_int_attr(frame, "pixel_type", _safe_int_attr(frame, "enPixelType")),
        "frame_len": _safe_int_attr(frame, "frame_len", _safe_int_attr(frame, "nFrameLen")),
    }


def open_camera(
    *,
    device_index: int = 0,
    exposure_us: int | None = None,
    gain: float | None = None,
) -> HikCameraController:
    cam = HikCameraController(device_index=device_index)
    cam.open()

    if exposure_us is not None:
        try:
            cam.set_exposure_time(exposure_us)
        except Exception:
            pass

    if gain is not None:
        try:
            cam.set_gain(gain)
        except Exception:
            pass

    return cam


def close_camera(cam: HikCameraController | None) -> None:
    if cam is None:
        return
    try:
        cam.close()
    except Exception:
        pass


def capture_with_opened_camera(
    *,
    cam: HikCameraController,
    save_dir: str,
    filename_pattern: str,
    format_kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    save_dir_path = Path(save_dir)
    save_dir_path.mkdir(parents=True, exist_ok=True)

    filename = build_image_name(filename_pattern, format_kwargs=format_kwargs)
    save_path = save_dir_path / filename

    raw_frame = cam.capture_once(str(save_path))
    frame_dict = frameinfo_to_dict(raw_frame, str(save_path))
    return {
        "saved_path": str(save_path),
        "frame": frame_dict,
    }


def capture_single_image(
    *,
    save_dir: str,
    filename_pattern: str,
    format_kwargs: Dict[str, Any],
    device_index: int = 0,
    exposure_us: int | None = None,
    gain: float | None = None,
) -> Dict[str, Any]:
    cam = None
    try:
        cam = open_camera(
            device_index=device_index,
            exposure_us=exposure_us,
            gain=gain,
        )
        return capture_with_opened_camera(
            cam=cam,
            save_dir=save_dir,
            filename_pattern=filename_pattern,
            format_kwargs=format_kwargs,
        )
    finally:
        close_camera(cam)
