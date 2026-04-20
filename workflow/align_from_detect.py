from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from workflow.stage_executor import move_to_absolute  # type: ignore


def load_json(path: str | Path) -> Dict[str, Any]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def find_image_and_clone(
    detect_data: Dict[str, Any],
    image_index: int,
    clone_id: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    for image in detect_data.get("images", []):
        if int(image.get("index", -1)) != int(image_index):
            continue

        for clone in image.get("clones", []):
            if str(clone.get("clone_id")) == str(clone_id):
                return image, clone

        raise ValueError(f"在 image_index={image_index} 中未找到 clone_id={clone_id}")

    raise ValueError(f"未找到 image_index={image_index}")


def compute_one_step_target(
    detect_data: Dict[str, Any],
    image: Dict[str, Any],
    clone: Dict[str, Any],
) -> Dict[str, Any]:
    ref = detect_data["reference"]
    scan_cfg = detect_data["scan_config"]

    pulses_per_mm = float(ref["pulses_per_mm"])
    x_sign = int(ref["x_stage_sign_for_view_down"])
    y_sign = int(ref["y_stage_sign_for_view_right"])

    fov_w_mm = float(scan_cfg["fov_mm"]["width"])
    fov_h_mm = float(scan_cfg["fov_mm"]["height"])

    image_width_px = int(image["image_width_px"])
    image_height_px = int(image["image_height_px"])

    stage_x_actual = int(image["stage_x_actual"])
    stage_y_actual = int(image["stage_y_actual"])

    dx_px = int(clone["offset_from_image_center_px"][0])  # 图像右正左负
    dy_px = int(clone["offset_from_image_center_px"][1])  # 图像下正上负

    mm_per_px_x = fov_w_mm / float(image_width_px)
    mm_per_px_y = fov_h_mm / float(image_height_px)

    # 图像上下对应 X；图像左右对应 Y；符号规则由 reference 给出
    delta_x_pulse = round(x_sign * dy_px * mm_per_px_y * pulses_per_mm)
    delta_y_pulse = round(y_sign * dx_px * mm_per_px_x * pulses_per_mm)

    aligned_stage_x = stage_x_actual + delta_x_pulse
    aligned_stage_y = stage_y_actual + delta_y_pulse

    return {
        "source_image_index": int(image["index"]),
        "source_row_index": int(image["row_index"]),
        "source_col_index": int(image["col_index"]),
        "source_image_path": image["image_path"],
        "selected_clone_id": clone["clone_id"],
        "selected_clone_center_px": clone["center_px"],
        "selected_clone_offset_from_center_px": clone["offset_from_image_center_px"],
        "source_stage_x_actual": stage_x_actual,
        "source_stage_y_actual": stage_y_actual,
        "mm_per_pixel": {
            "x": mm_per_px_x,
            "y": mm_per_px_y,
        },
        "alignment_delta_pulse": {
            "x": int(delta_x_pulse),
            "y": int(delta_y_pulse),
        },
        "aligned_stage_x": int(aligned_stage_x),
        "aligned_stage_y": int(aligned_stage_y),
    }


def run_align_from_detect_task(task: Dict[str, Any]) -> Dict[str, Any]:
    detect_json = task["detect_json"]
    image_index = int(task["image_index"])
    clone_id = str(task["clone_id"])
    dry_run = bool(task.get("dry_run", False))

    motion = task.get("motion", {}) or {}
    output_json = task.get("output_json")

    detect_data = load_json(detect_json)
    image, clone = find_image_and_clone(
        detect_data=detect_data,
        image_index=image_index,
        clone_id=clone_id,
    )
    plan = compute_one_step_target(detect_data, image, clone)

    result: Dict[str, Any] = {
        "status": "planned" if dry_run else "success",
        "task_type": "align_clone_one_step_from_detect",
        "pick_ready": True,
        "note": "仅表示已根据 detect_result.json 计算出一步到位对中位置；未做去重、边缘安全、tip可达性判定。",
        "detect_json": str(detect_json),
        **plan,
    }

    if not dry_run:
        move_result = move_to_absolute(
            port=motion.get("port", "COM3"),
            x_target=plan["aligned_stage_x"],
            y_target=plan["aligned_stage_y"],
            profile_vel=int(motion.get("profile_vel", 200000)),
            profile_acc=int(motion.get("profile_acc", 50000)),
            profile_dec=int(motion.get("profile_dec", 50000)),
            x_slave=int(motion.get("x_slave", 1)),
            y_slave=int(motion.get("y_slave", 2)),
            baudrate=int(motion.get("baudrate", 115200)),
            settle_s=float(motion.get("settle_s", 0.8)),
        )
        result["stage_move_result"] = move_result

    if output_json:
        out_path = Path(output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    return result
