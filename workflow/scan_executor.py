from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from workflow.camera_executor import (
    open_camera,
    close_camera,
    capture_with_opened_camera,
)
from workflow.stage_executor import move_to_absolute


def _format_kwargs(params: Dict, point: Dict) -> Dict:
    return {
        "task_id": params["task_id"],
        "well": params["well_name"],
        "index": int(point["index"]),
        "row": int(point["row_index"]),
        "col": int(point["col_index"]),
        "vdown": float(point["view_down_mm"]),
        "vright": float(point["view_right_mm"]),
        "x": int(point["stage_x_target"]),
        "y": int(point["stage_y_target"]),
    }


def execute_scan_capture(ctx: Dict, params: Dict, plan: Dict) -> Dict:
    motion = params["motion"]
    captures: List[Dict] = []

    cam = None
    try:
        cam = open_camera(
            device_index=int(params["device_index"]),
            exposure_us=params.get("exposure_us"),
            gain=params.get("gain"),
        )

        for point in plan["points"]:
            motion_result = move_to_absolute(
                port=motion.get("port", "COM3"),
                x_target=int(point["stage_x_target"]),
                y_target=int(point["stage_y_target"]),
                profile_vel=int(motion["profile_vel"]),
                profile_acc=int(motion["profile_acc"]),
                profile_dec=int(motion["profile_dec"]),
                x_slave=int(motion.get("x_slave", 1)),
                y_slave=int(motion.get("y_slave", 2)),
                baudrate=int(motion.get("baudrate", 115200)),
                settle_s=float(params["settle_s"]),
            )

            capture_result = capture_with_opened_camera(
                cam=cam,
                save_dir=params["save_dir"],
                filename_pattern=params["filename_pattern"],
                format_kwargs=_format_kwargs(params, point),
            )

            captures.append(
                {
                    **point,
                    "motion_result": motion_result,
                    "capture_result": capture_result,
                }
            )
    finally:
        close_camera(cam)

    result = {
        "task_id": params["task_id"],
        "status": "success",
        "task_type": params["task_type"],
        "plate_type": params["plate_type"],
        "well_name": params["well_name"],
        "objective_name": params["objective_name"],
        "reference": plan["reference"],
        "scan_config": plan["scan_config"],
        "image_count": len(captures),
        "captures": captures,
    }

    scan_output_json = params.get("scan_output_json")
    if scan_output_json:
        out_path = Path(scan_output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    return result
