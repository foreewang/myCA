from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict

from PIL import Image


def _build_manual_detect_result(
    *,
    image_path: Path,
    cx: float,
    cy: float,
    stage_x: int,
    stage_y: int,
    plate_type: str,
    well_name: str | None,
    objective_name: str,
    fov_width_mm: float,
    fov_height_mm: float,
    clone_id: str,
    image_index: int,
) -> Dict[str, Any]:
    with Image.open(image_path) as img:
        width, height = img.size

    center_x = (width - 1) / 2.0
    center_y = (height - 1) / 2.0
    dx_px = float(cx) - center_x
    dy_px = float(cy) - center_y
    mm_per_x = float(fov_width_mm) / float(width)
    mm_per_y = float(fov_height_mm) / float(height)
    dx_mm = dx_px * mm_per_x
    dy_mm = dy_px * mm_per_y

    clone = {
        "clone_id": clone_id,
        "target_id": clone_id,
        "rank": 1,
        "center_pixel": [float(cx), float(cy)],
        "center_pixel_xy": {"x": float(cx), "y": float(cy)},
        "offset_from_image_center_px": [dx_px, dy_px],
        "offset_from_image_center_px_xy": {"x": dx_px, "y": dy_px},
        "offset_from_image_center_mm": [dx_mm, dy_mm],
        "offset_from_image_center_mm_xy": {"x": dx_mm, "y": dy_mm},
        "area_px": 1.0,
        "area": 1.0,
        "score": 1.0,
        "source": "manual_annotation_from_stage",
    }

    image_item = {
        "image_index": int(image_index),
        "index": int(image_index),
        "image_id": image_path.stem,
        "image_name": image_path.name,
        "image_path": str(image_path),
        "width": width,
        "height": height,
        "fov_mm": {"width": float(fov_width_mm), "height": float(fov_height_mm)},
        "image_center_pixel": [center_x, center_y],
        "mm_per_pixel": {"x": mm_per_x, "y": mm_per_y},
        "stage_x_target": int(stage_x),
        "stage_y_target": int(stage_y),
        "stage_x_actual": int(stage_x),
        "stage_y_actual": int(stage_y),
        "row_index": None,
        "col_index": None,
        "view_down_mm": None,
        "view_right_mm": None,
        "clones": [clone],
        "candidates": [clone],
        "components": [clone],
    }

    return {
        "task_id": "manual_detect_from_stage",
        "status": "success",
        "task_type": "detect",
        "plate_type": plate_type,
        "well_name": well_name,
        "objective_name": objective_name,
        "fov_mm": {"width": float(fov_width_mm), "height": float(fov_height_mm)},
        "resolution": {"width": width, "height": height},
        "mm_per_pixel": {"x": mm_per_x, "y": mm_per_y},
        "images": [image_item],
        "source": {
            "type": "manual_annotation_from_stage",
            "stage_x_actual": int(stage_x),
            "stage_y_actual": int(stage_y),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build manual detect_result.json from a clicked point and known stage coordinates."
    )
    parser.add_argument("--image", required=True, help="Image path used for manual annotation")
    parser.add_argument("--cx", required=True, type=float, help="Clicked target center x in pixels")
    parser.add_argument("--cy", required=True, type=float, help="Clicked target center y in pixels")
    parser.add_argument("--stage-x", required=True, type=int, help="Stage X actual coordinate when the image was captured")
    parser.add_argument("--stage-y", required=True, type=int, help="Stage Y actual coordinate when the image was captured")
    parser.add_argument("--plate-type", required=True)
    parser.add_argument("--well", default=None)
    parser.add_argument("--objective", default="4x")
    parser.add_argument("--fov-width-mm", required=True, type=float)
    parser.add_argument("--fov-height-mm", required=True, type=float)
    parser.add_argument("--clone-id", default="C_MANUAL_002")
    parser.add_argument("--image-index", type=int, default=1)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    image_path = Path(args.image)
    if not image_path.exists():
        raise FileNotFoundError(image_path)

    result = _build_manual_detect_result(
        image_path=image_path,
        cx=args.cx,
        cy=args.cy,
        stage_x=args.stage_x,
        stage_y=args.stage_y,
        plate_type=args.plate_type,
        well_name=args.well,
        objective_name=args.objective,
        fov_width_mm=args.fov_width_mm,
        fov_height_mm=args.fov_height_mm,
        clone_id=args.clone_id,
        image_index=args.image_index,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    clone = result["images"][0]["clones"][0]
    print(f"manual detect result written: {out_path}")
    print(f"image = {image_path}")
    print(f"stage_actual = [{args.stage_x}, {args.stage_y}]")
    print(f"image_center = {result['images'][0]['image_center_pixel']}")
    print(f"clicked_center = [{args.cx}, {args.cy}]")
    print(f"offset_px = {clone['offset_from_image_center_px']}, norm={math.hypot(*clone['offset_from_image_center_px']):.3f}")
    print(f"offset_mm = {clone['offset_from_image_center_mm']}, norm={math.hypot(*clone['offset_from_image_center_mm']):.6f}")


if __name__ == "__main__":
    main()
