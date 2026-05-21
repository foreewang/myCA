from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEVICES_DIR = PROJECT_ROOT / "devices"
if str(DEVICES_DIR) not in sys.path:
    sys.path.insert(0, str(DEVICES_DIR))

from camera_controller import HikCameraController  # type: ignore


def _file_info(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    return {
        "path": str(p),
        "exists": p.exists(),
        "size_bytes": p.stat().st_size if p.exists() else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test Hikvision MVS camera photo capture and SDK recording."
    )
    parser.add_argument("--mvs-python-dir", default=None)
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--serial-number", default=None)
    parser.add_argument("--exposure-us", type=float, default=None)
    parser.add_argument("--gain", type=float, default=None)
    parser.add_argument("--photo-path", default="data/camera_tests/test_capture.bmp")
    parser.add_argument("--video-path", default="data/camera_tests/test_record.avi")
    parser.add_argument("--duration-s", type=float, default=5.0)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--bitrate-kbps", type=int, default=1000)
    parser.add_argument("--timeout-ms", type=int, default=None)
    parser.add_argument("--skip-photo", action="store_true")
    parser.add_argument("--skip-video", action="store_true")
    args = parser.parse_args()

    cam = HikCameraController(
        mvs_python_dir=args.mvs_python_dir,
        device_index=args.device_index,
        serial_number=args.serial_number,
        default_exposure_us=args.exposure_us,
        default_gain=args.gain,
    )

    result: dict[str, Any] = {
        "status": "started",
        "photo": None,
        "video": None,
        "started_at": time.time(),
    }

    try:
        cam.open()

        if not args.skip_video and not args.skip_photo:
            cam.start_background_recording(
                args.video_path,
                fps=args.fps,
                bitrate_kbps=args.bitrate_kbps,
                timeout_ms=args.timeout_ms,
            )
            time.sleep(min(max(args.duration_s * 0.25, 0.2), 1.0))
            photo_frame = cam.capture_once(args.photo_path, timeout_ms=args.timeout_ms)
            result["photo"] = {
                "frame": photo_frame.__dict__,
                "file": _file_info(args.photo_path),
            }
            time.sleep(max(args.duration_s - (time.time() - result["started_at"]), 0.0))
            video_info = cam.stop_background_recording()
            result["video"] = {
                "record": video_info.__dict__,
                "file": _file_info(video_info.saved_path),
            }

        elif not args.skip_photo:
            photo_frame = cam.capture_once(args.photo_path, timeout_ms=args.timeout_ms)
            result["photo"] = {
                "frame": photo_frame.__dict__,
                "file": _file_info(args.photo_path),
            }

        elif not args.skip_video:
            video_info = cam.record_video(
                args.video_path,
                duration_s=args.duration_s,
                fps=args.fps,
                bitrate_kbps=args.bitrate_kbps,
                timeout_ms=args.timeout_ms,
            )
            result["video"] = {
                "record": video_info.__dict__,
                "file": _file_info(video_info.saved_path),
            }

        result["status"] = "success"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        raise
    finally:
        try:
            cam.close()
        finally:
            result["finished_at"] = time.time()
            print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
