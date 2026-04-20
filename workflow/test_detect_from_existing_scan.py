from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image
from vision.vision.detect_pipeline import process_image


def offset_from_center(center_px: list[int], image_center_px: list[int]) -> list[int]:
    return [int(center_px[0] - image_center_px[0]), int(center_px[1] - image_center_px[1])]


def actual_stage_xy(capture: dict) -> tuple[int | None, int | None]:
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


def main() -> None:
    scan_result_path = Path(r"C:\colony_system\config\a1_scan_result.json")
    output_path = Path(r"C:\colony_system\config\a1_scan_detect_result_test.json")

    with scan_result_path.open("r", encoding="utf-8") as f:
        scan_result = json.load(f)

    images = []

    for capture in scan_result.get("captures", []):
        image_path = capture.get("capture_result", {}).get("saved_path")
        if not image_path:
            continue

        actual_x, actual_y = actual_stage_xy(capture)

        det = process_image(image_path)

        with Image.open(image_path) as im:
            width = int(im.width)
            height = int(im.height)

        image_center = [width // 2, height // 2]

        clones = []
        for i, comp in enumerate(det.get("components", []), start=1):
            center_px = [int(comp["center_pixel"][0]), int(comp["center_pixel"][1])]
            clones.append({
                "clone_id": comp.get("id", f"C{i:02d}"),
                "center_px": center_px,
                "offset_from_image_center_px": offset_from_center(center_px, image_center),
                "bbox": comp.get("bbox"),
                "area_px": comp.get("area_px"),
                "source_image_path": image_path,
                "stage_x_actual": actual_x,
                "stage_y_actual": actual_y,
            })

        images.append({
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
            "clone_count": int(det.get("component_count", 0)),
            "clones": clones,
        })

    result = {
        "task_id": scan_result.get("task_id", "scan_detect_test"),
        "status": "success",
        "task_type": "detect_from_existing_scan_result",
        "plate_type": scan_result.get("plate_type"),
        "well_name": scan_result.get("well_name"),
        "objective_name": scan_result.get("objective_name"),
        "reference": scan_result.get("reference"),
        "scan_config": scan_result.get("scan_config"),
        "source_scan_result_json": str(scan_result_path),
        "image_count": len(images),
        "total_clone_count": sum(int(x["clone_count"]) for x in images),
        "images": images,
    }

    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved: {output_path}")
    print(f"image_count={result['image_count']}, total_clone_count={result['total_clone_count']}")


if __name__ == "__main__":
    main()