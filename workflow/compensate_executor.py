from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from workflow.plate_geometry import get_pulses_per_mm, get_view_signs
from workflow.stage_executor import move_to_absolute


def _image_center_distance2(image_item: Dict[str, Any], clone_item: Dict[str, Any]) -> float:
    dx, dy = clone_item.get("offset_from_image_center_px", [0, 0])
    return float(dx * dx + dy * dy)



def _all_clone_refs(detect_result: Dict[str, Any]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    refs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for image_item in detect_result.get("images", []):
        for clone_item in image_item.get("clones", []):
            refs.append((image_item, clone_item))
    return refs



def select_clone_for_compensation(detect_result: Dict[str, Any], selector_cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    从 detect_result 中选择一个克隆作为补偿目标。

    支持模式：
    - first: 第一张图第一个克隆
    - largest_area: 所有图中面积最大的克隆
    - nearest_image_center: 所有图中距离图像中心最近的克隆
    - clone_id: 按 clone_id 匹配；可选 image_index 进一步限定
    - image_and_clone: 显式指定 image_index + clone_id
    """
    mode = str((selector_cfg or {}).get("mode") or "first").strip().lower()
    refs = _all_clone_refs(detect_result)
    if not refs:
        raise ValueError("detect_result 中没有可补偿的克隆")

    if mode == "first":
        return refs[0]

    if mode == "largest_area":
        return max(refs, key=lambda t: float((t[1].get("area_px") or 0)))

    if mode == "nearest_image_center":
        return min(refs, key=lambda t: _image_center_distance2(t[0], t[1]))

    if mode == "clone_id":
        clone_id = str(selector_cfg.get("clone_id") or "").strip()
        image_index = selector_cfg.get("image_index")
        for image_item, clone_item in refs:
            if clone_item.get("clone_id") != clone_id:
                continue
            if image_index is not None and int(image_item.get("index")) != int(image_index):
                continue
            return image_item, clone_item
        raise ValueError(f"未找到 clone_id={clone_id!r} 对应的克隆")

    if mode == "image_and_clone":
        image_index = int(selector_cfg["image_index"])
        clone_id = str(selector_cfg["clone_id"])
        for image_item, clone_item in refs:
            if int(image_item.get("index")) == image_index and clone_item.get("clone_id") == clone_id:
                return image_item, clone_item
        raise ValueError(f"未找到 image_index={image_index}, clone_id={clone_id!r} 对应的克隆")

    raise ValueError(f"不支持的 compensate.selector.mode: {mode}")



def execute_compensate_on_detect_result(ctx: Dict[str, Any], params: Dict[str, Any], detect_result: Dict[str, Any]) -> Dict[str, Any]:
    """根据选定克隆相对图像中心的偏差，计算并执行位移台补偿。"""
    selector_cfg = params.get("compensate_selector", {}) or {}
    image_item, clone_item = select_clone_for_compensation(detect_result, selector_cfg)

    plate_cfg = ctx["plate"]
    ppm = float(get_pulses_per_mm(plate_cfg))
    x_sign, y_sign = get_view_signs(plate_cfg)

    offset_px = clone_item.get("offset_from_image_center_px", [0, 0])
    mm_per_pixel = image_item["mm_per_pixel"]
    offset_right_mm = float(offset_px[0]) * float(mm_per_pixel["x"])
    offset_down_mm = float(offset_px[1]) * float(mm_per_pixel["y"])

    base_x = image_item.get("stage_x_actual")
    base_y = image_item.get("stage_y_actual")
    if base_x is None:
        base_x = image_item.get("stage_x_target")
    if base_y is None:
        base_y = image_item.get("stage_y_target")
    if base_x is None or base_y is None:
        raise ValueError("无法确定补偿基准坐标：stage_x/stage_y 缺失")

    target_x = int(round(float(base_x) + x_sign * offset_down_mm * ppm))
    target_y = int(round(float(base_y) + y_sign * offset_right_mm * ppm))

    motion = params["motion"]
    move_result = move_to_absolute(
        port=motion.get("port", "COM3"),
        x_target=target_x,
        y_target=target_y,
        profile_vel=int(motion["profile_vel"]),
        profile_acc=int(motion["profile_acc"]),
        profile_dec=int(motion["profile_dec"]),
        x_slave=int(motion.get("x_slave", 1)),
        y_slave=int(motion.get("y_slave", 2)),
        baudrate=int(motion.get("baudrate", 115200)),
        settle_s=float(params.get("settle_s", 0.8)),
    )

    result = {
        "task_id": params["task_id"],
        "status": "success",
        "task_type": "compensate",
        "plate_type": params["plate_type"],
        "well_name": params["well_name"],
        "objective_name": params["objective_name"],
        "selector": selector_cfg,
        "selected_image_index": int(image_item["index"]),
        "selected_clone": clone_item,
        "base_stage": {
            "x": int(base_x),
            "y": int(base_y),
        },
        "offset_from_image_center_px": [int(offset_px[0]), int(offset_px[1])],
        "offset_mm": {
            "view_right_mm": offset_right_mm,
            "view_down_mm": offset_down_mm,
        },
        "compensate_target": {
            "x": target_x,
            "y": target_y,
        },
        "move_result": move_result,
    }

    output_json = params.get("compensate_output_json")
    if output_json:
        out_path = Path(output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    return result
