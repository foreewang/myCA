#人工标注结果生成工具：根据result.json中的capture_result.captures信息和点击的图像坐标，生成detect_result.json文件，供后续分析使用。适用于没有stage坐标但有capture_result的情况。

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image


def _load_json(path: str | Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _candidate_capture_results(root: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return all capture_result dicts from either single-well or well-list result.json."""
    out: List[Dict[str, Any]] = []

    cap = root.get("capture_result")
    if isinstance(cap, dict) and isinstance(cap.get("captures"), list):
        out.append(cap)

    for well in root.get("wells", []) or []:
        if not isinstance(well, dict):
            continue
        result = well.get("result") or {}
        cap = result.get("capture_result")
        if isinstance(cap, dict) and isinstance(cap.get("captures"), list):
            out.append(cap)

    return out


def _norm_path(p: Any) -> str:
    return str(p or "").replace("/", "\\").lower()


def _find_capture_item(result_json: Dict[str, Any], image_path: Path, image_index: Optional[int]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    image_name = image_path.name
    image_path_norm = _norm_path(image_path)

    candidates = _candidate_capture_results(result_json)
    if not candidates:
        raise ValueError("result.json 中没有找到 capture_result.captures。请确认传入的是任务总 result.json。")

    # 1) Prefer exact saved_path match.
    for cap_result in candidates:
        for item in cap_result.get("captures", []) or []:
            saved = (((item.get("capture_result") or {}).get("saved_path")) or "")
            if _norm_path(saved) == image_path_norm:
                return cap_result, item

    # 2) Match by image name.
    for cap_result in candidates:
        for item in cap_result.get("captures", []) or []:
            saved = (((item.get("capture_result") or {}).get("saved_path")) or "")
            if Path(saved).name == image_name:
                return cap_result, item

    # 3) Match by index if explicitly provided.
    if image_index is not None:
        for cap_result in candidates:
            for item in cap_result.get("captures", []) or []:
                if int(item.get("index")) == int(image_index):
                    return cap_result, item

    raise ValueError(
        f"无法在 result.json 中匹配图像: {image_path}. "
        f"请检查图像路径是否来自该 result.json，或显式传入 --image-index。"
    )


def _get_actual_stage(item: Dict[str, Any], axis: str) -> Optional[int]:
    motion = item.get("motion_result") or {}
    after = motion.get("after") or {}
    axis_info = after.get(axis) or {}
    value = axis_info.get("current_pos")
    if value is None:
        return None
    return int(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build manual detect_result.json from task result.json and a clicked point.")
    parser.add_argument("--image", required=True, help="Manually annotated image path")
    parser.add_argument("--result", required=True, help="Task result.json that contains capture_result.captures")
    parser.add_argument("--cx", required=True, type=float, help="Clicked target center x in pixels")
    parser.add_argument("--cy", required=True, type=float, help="Clicked target center y in pixels")
    parser.add_argument("--clone-id", default="C_MANUAL_001")
    parser.add_argument("--image-index", type=int, default=None, help="Optional capture index, e.g. 2 for D4_002")
    parser.add_argument("--well", default=None)
    parser.add_argument("--objective", default=None)
    parser.add_argument("--plate-type", default=None)
    parser.add_argument("--fov-width-mm", type=float, default=None)
    parser.add_argument("--fov-height-mm", type=float, default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    image_path = Path(args.image)
    if not image_path.exists():
        raise FileNotFoundError(image_path)

    root = _load_json(args.result)
    cap_result, capture_item = _find_capture_item(root, image_path, args.image_index)

    with Image.open(image_path) as img:
        width, height = img.size

    scan_config = cap_result.get("scan_config") or {}
    fov_cfg = scan_config.get("fov_mm") or {}
    fov_w = float(args.fov_width_mm if args.fov_width_mm is not None else fov_cfg.get("width", 3.0))
    fov_h = float(args.fov_height_mm if args.fov_height_mm is not None else fov_cfg.get("height", 3.0))

    center_x = (width - 1) / 2.0
    center_y = (height - 1) / 2.0
    dx_px = float(args.cx) - center_x
    dy_px = float(args.cy) - center_y
    mm_per_x = fov_w / width
    mm_per_y = fov_h / height
    dx_mm = dx_px * mm_per_x
    dy_mm = dy_px * mm_per_y

    stage_x_target = capture_item.get("stage_x_target")
    stage_y_target = capture_item.get("stage_y_target")
    stage_x_actual = _get_actual_stage(capture_item, "x")
    stage_y_actual = _get_actual_stage(capture_item, "y")

    if stage_x_target is None or stage_y_target is None:
        motion = capture_item.get("motion_result") or {}
        target = motion.get("target") or {}
        stage_x_target = target.get("x")
        stage_y_target = target.get("y")

    if stage_x_target is None or stage_y_target is None:
        raise ValueError("匹配到的 capture item 仍缺少 stage_x_target/stage_y_target，无法生成补偿基准。")

    image_index = int(capture_item.get("index", args.image_index or 1))
    well_name = args.well or cap_result.get("well_name") or root.get("well_name")
    objective_name = args.objective or cap_result.get("objective_name") or root.get("objective_name")
    plate_type = args.plate_type or cap_result.get("plate_type") or root.get("plate_type")

    clone = {
        "clone_id": args.clone_id,
        "target_id": args.clone_id,
        "rank": 1,
        "center_pixel": [float(args.cx), float(args.cy)],
        "center_pixel_xy": {"x": float(args.cx), "y": float(args.cy)},
        "offset_from_image_center_px": [dx_px, dy_px],
        "offset_from_image_center_px_xy": {"x": dx_px, "y": dy_px},
        "offset_from_image_center_mm": [dx_mm, dy_mm],
        "offset_from_image_center_mm_xy": {"x": dx_mm, "y": dy_mm},
        "area_px": 1.0,
        "area": 1.0,
        "score": 1.0,
        "source": "manual_annotation",
    }

    image_item = {
        "image_index": image_index,
        "index": image_index,
        "image_id": image_path.stem,
        "image_name": image_path.name,
        "image_path": str(image_path),
        "width": width,
        "height": height,
        "fov_mm": {"width": fov_w, "height": fov_h},
        "image_center_pixel": [center_x, center_y],
        "mm_per_pixel": {"x": mm_per_x, "y": mm_per_y},
        "stage_x_target": int(stage_x_target),
        "stage_y_target": int(stage_y_target),
        "stage_x_actual": int(stage_x_actual) if stage_x_actual is not None else None,
        "stage_y_actual": int(stage_y_actual) if stage_y_actual is not None else None,
        "row_index": capture_item.get("row_index"),
        "col_index": capture_item.get("col_index"),
        "view_down_mm": capture_item.get("view_down_mm"),
        "view_right_mm": capture_item.get("view_right_mm"),
        "clones": [clone],
        "candidates": [clone],
        "components": [clone],
    }

    result = {
        "task_id": "manual_detect_for_compensate_eval",
        "status": "success",
        "task_type": "detect",
        "plate_type": plate_type,
        "well_name": well_name,
        "objective_name": objective_name,
        "fov_mm": {"width": fov_w, "height": fov_h},
        "resolution": {"width": width, "height": height},
        "mm_per_pixel": {"x": mm_per_x, "y": mm_per_y},
        "images": [image_item],
        "source": {
            "type": "manual_annotation_from_task_result",
            "result_json": str(Path(args.result)),
            "matched_capture_index": image_index,
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    base_x = stage_x_actual if stage_x_actual is not None else stage_x_target
    base_y = stage_y_actual if stage_y_actual is not None else stage_y_target
    print(f"manual detect result written: {out_path}")
    print(f"matched image_index = {image_index}")
    print(f"well_name = {well_name}")
    print(f"stage_x_target = {stage_x_target}")
    print(f"stage_y_target = {stage_y_target}")
    print(f"stage_x_actual = {stage_x_actual}")
    print(f"stage_y_actual = {stage_y_actual}")
    print(f"base_stage_used_by_compensate = [{base_x}, {base_y}]")
    print(f"image_center = [{center_x:.3f}, {center_y:.3f}]")
    print(f"clicked_center = [{args.cx:.3f}, {args.cy:.3f}]")
    print(f"offset_px = [{dx_px:.3f}, {dy_px:.3f}], norm={math.hypot(dx_px, dy_px):.3f}")
    print(f"offset_mm = [{dx_mm:.6f}, {dy_mm:.6f}], norm={math.hypot(dx_mm, dy_mm):.6f}")


if __name__ == "__main__":
    main()

