from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image

from workflow.detect_api import run_detect_on_image


def _image_size(image_path: str) -> tuple[int, int]:
    with Image.open(image_path) as im:
        return int(im.width), int(im.height)


def _actual_stage_xy(capture: Dict[str, Any]) -> tuple[int | None, int | None]:
    motion_result = capture.get("motion_result", {}) or {}
    after = motion_result.get("after", {}) or {}
    x = (((after.get("x") or {}).get("current_pos")))
    y = (((after.get("y") or {}).get("current_pos")))
    try:
        x = int(x) if x is not None else None
    except Exception:
        x = None
    try:
        y = int(y) if y is not None else None
    except Exception:
        y = None
    return x, y


def _offset_from_center(center_px: List[int], image_center_px: List[int]) -> List[int]:
    return [int(center_px[0] - image_center_px[0]), int(center_px[1] - image_center_px[1])]


def execute_detect_on_scan_result(ctx: Dict[str, Any], params: Dict[str, Any], scan_result: Dict[str, Any]) -> Dict[str, Any]:
    detect_cfg = ctx["task"].get("detect", {}) or {}
    entrypoint = detect_cfg.get("entrypoint")

    fov_cfg = scan_result.get("scan_config", {}).get("fov_mm", {}) or {}
    fov_w_mm = float(fov_cfg.get("width"))
    fov_h_mm = float(fov_cfg.get("height"))

    images: List[Dict[str, Any]] = []

    for capture in scan_result.get("captures", []):
        image_path = capture.get("capture_result", {}).get("saved_path")
        if not image_path:
            continue

        width, height = _image_size(image_path)
        image_center = [width // 2, height // 2]
        mm_per_pixel = {
            "x": fov_w_mm / float(width),
            "y": fov_h_mm / float(height),
        }

        actual_x, actual_y = _actual_stage_xy(capture)
        detect_result = run_detect_on_image(image_path, entrypoint=entrypoint)

        clones: List[Dict[str, Any]] = []
        for i, comp in enumerate(detect_result.get("components", []), start=1):
            center_px = [int(comp["center_pixel"][0]), int(comp["center_pixel"][1])]
            offset_px = _offset_from_center(center_px, image_center)

            clone_out = {
                "clone_id": comp.get("id", f"C{i:02d}"),
                "center_px": center_px,
                "offset_from_image_center_px": offset_px,
                "bbox": comp.get("bbox"),
                "area_px": comp.get("area_px"),
                "source_image_path": image_path,
                "stage_x_actual": actual_x,
                "stage_y_actual": actual_y,
            }
            clones.append(clone_out)

        images.append(
            {
                "index": int(capture["index"]),
                "row_index": int(capture["row_index"]),
                "col_index": int(capture["col_index"]),
                "image_path": image_path,
                "stage_x_target": int(capture["stage_x_target"]),
                "stage_y_target": int(capture["stage_y_target"]),
                "stage_x_actual": actual_x,
                "stage_y_actual": actual_y,
                "image_width_px": width,
                "image_height_px": height,
                "image_center_px": image_center,
                "mm_per_pixel": mm_per_pixel,
                "clone_count": int(detect_result.get("component_count", 0)),
                "clones": clones,
            }
        )

    total_clones = sum(int(x["clone_count"]) for x in images)

    result = {
        "task_id": params["task_id"],
        "status": "success",
        "task_type": "single_well_scan_and_detect",
        "plate_type": params["plate_type"],
        "well_name": params["well_name"],
        "objective_name": params["objective_name"],
        "reference": scan_result.get("reference"),
        "scan_config": scan_result.get("scan_config"),
        "scan_result_json": params.get("scan_result_json"),
        "image_count": len(images),
        "total_clone_count": total_clones,
        "images": images,
    }

    output_json = params.get("detect_output_json")
    if output_json:
        out_path = Path(output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    return result