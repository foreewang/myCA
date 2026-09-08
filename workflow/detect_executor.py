"""对扫描结果中的图片执行检测、计算克隆偏移并生成可视化叠加图。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from PIL import Image, ImageDraw

from workflow.clone_dedupe import dedupe_well_clones
from workflow.detect_api import run_detect_on_image
from workflow.file_io import atomic_write_json
from workflow.task_control import raise_if_cancel_requested, report_progress


_RULE_PATH_ENTRYPOINTS = {
    f"{module}:process_image"
    for module in ("vision.vision.detect_pipeline", "vision.detect_pipeline")
}


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


def _to_int_pair(value: Any) -> Tuple[int, int] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 2:
        try:
            return int(round(float(value[0]))), int(round(float(value[1])))
        except Exception:
            return None
    return None


def _extract_polygon_points(raw_item: Dict[str, Any]) -> List[Tuple[int, int]] | None:
    """
    尝试从视觉原始结果中提取轮廓/多边形点。
    支持常见字段：
    - contour / contours
    - polygon
    - outline
    - points
    - cnt

    返回值：
    - [(x1, y1), (x2, y2), ...]
    - 若无法识别则返回 None
    """
    if not isinstance(raw_item, dict):
        return None

    candidate_keys = ("contour_points", "contour", "contours", "polygon", "outline", "points", "cnt")
    raw_points = None
    for key in candidate_keys:
        if key in raw_item:
            raw_points = raw_item[key]
            break

    if raw_points is None:
        return None

    # 兼容 [[[x,y]], [[x,y]], ...] 这种 OpenCV 风格
    if isinstance(raw_points, Sequence) and not isinstance(raw_points, (str, bytes)):
        pts: List[Tuple[int, int]] = []
        for item in raw_points:
            # 先尝试 [x, y]
            pair = _to_int_pair(item)
            if pair is not None:
                pts.append(pair)
                continue

            # 再尝试 [[x, y]]
            if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) >= 1:
                pair = _to_int_pair(item[0])
                if pair is not None:
                    pts.append(pair)

        if len(pts) >= 3:
            return pts

    return None


def _choose_overlay_dir(params: Dict[str, Any], image_path: str) -> Path:
    """
    识别结果标记图默认保存策略：
    1. 若 task.detect.overlay_dir 有配置，则优先使用；
    2. 否则若 detect_output_json 已配置，则保存到 detect_output_json 同级 detect_overlays 目录；
    3. 否则保存到原图所在目录下的 detect_overlays 目录。
    """
    overlay_dir = params.get("detect_overlay_dir")
    if overlay_dir:
        out_dir = Path(overlay_dir)
    elif params.get("detect_output_json"):
        out_dir = Path(params["detect_output_json"]).parent / "detect_overlays"
    else:
        out_dir = Path(image_path).parent / "detect_overlays"

    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _overlay_path_for_image(image_path: str, overlay_dir: Path) -> Path:
    src = Path(image_path)
    return overlay_dir / f"{src.stem}_detect_overlay.png"


def _vision_output_dir_for_image(image_path: str, overlay_dir: Path) -> Path:
    src = Path(image_path)
    return overlay_dir / f"{src.stem}_vision"


def _vision_overlay_path(output_dir: Path) -> Path:
    return output_dir / "06_overlay.bmp"


def _build_scale_bar_config(detect_cfg: Dict[str, Any], mm_per_pixel: Dict[str, float]) -> Dict[str, Any] | None:
    cfg = detect_cfg.get("scale_bar")
    if cfg is None:
        return None
    if isinstance(cfg, bool):
        if not cfg:
            return None
        out: Dict[str, Any] = {"enabled": True}
    elif isinstance(cfg, dict):
        if not bool(cfg.get("enabled", True)):
            return None
        out = dict(cfg)
        out["enabled"] = True
    else:
        return None

    out["mm_per_pixel"] = float(mm_per_pixel["x"])
    out["mm_per_pixel_x"] = float(mm_per_pixel["x"])
    out["mm_per_pixel_y"] = float(mm_per_pixel["y"])
    return out


def _draw_center_mark(draw: ImageDraw.ImageDraw, center: Tuple[int, int], radius: int = 8) -> None:
    x, y = center
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline="red", width=2)
    draw.line((x - radius - 6, y, x + radius + 6, y), fill="red", width=2)
    draw.line((x, y - radius - 6, x, y + radius + 6), fill="red", width=2)


def _draw_image_center(draw: ImageDraw.ImageDraw, center: Tuple[int, int], radius: int = 10) -> None:
    x, y = center
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline="cyan", width=2)
    draw.line((x - 16, y, x + 16, y), fill="cyan", width=2)
    draw.line((x, y - 16, x, y + 16), fill="cyan", width=2)


def _render_overlay_image(
    image_path: str,
    image_center_px: List[int],
    raw_clones: List[Dict[str, Any]],
    overlay_path: Path,
    draw_bbox: bool = True,
    draw_center: bool = True,
    draw_image_center: bool = True,
) -> str:
    with Image.open(image_path) as im:
        canvas = im.convert("RGB")
        draw = ImageDraw.Draw(canvas)

        if draw_image_center:
            _draw_image_center(draw, (int(image_center_px[0]), int(image_center_px[1])))

        for clone in raw_clones:
            clone_id = str(clone.get("clone_id") or "clone")
            center_px = clone.get("center_px")
            bbox = clone.get("bbox")
            raw = clone.get("raw") or {}

            polygon = _extract_polygon_points(raw)
            if polygon:
                draw.line(polygon + [polygon[0]], fill="lime", width=3)

            if draw_bbox and bbox and len(bbox) >= 4:
                x, y, w, h = [int(v) for v in bbox[:4]]
                draw.rectangle((x, y, x + w, y + h), outline="yellow", width=3)
                draw.text((x + 4, max(0, y - 16)), clone_id, fill="yellow")

            if draw_center and center_px and len(center_px) >= 2:
                _draw_center_mark(draw, (int(center_px[0]), int(center_px[1])))
                draw.text((int(center_px[0]) + 10, int(center_px[1]) + 10), clone_id, fill="red")

        canvas.save(overlay_path)
    return str(overlay_path)


def execute_detect_on_scan_result(ctx: Dict[str, Any], params: Dict[str, Any], scan_result: Dict[str, Any]) -> Dict[str, Any]:
    detect_cfg = ctx["task"].get("detect", {}) or {}
    entrypoint = detect_cfg.get("entrypoint")
    save_debug = detect_cfg.get("save_debug", False)
    if not isinstance(save_debug, bool):
        raise ValueError("detect.save_debug must be a boolean")

    save_overlay = bool(detect_cfg.get("save_overlay", True))
    overlay_source = str(detect_cfg.get("overlay_source", "vision")).strip().lower()
    draw_bbox = bool(detect_cfg.get("draw_bbox", True))
    draw_center = bool(detect_cfg.get("draw_center", True))
    draw_image_center = bool(detect_cfg.get("draw_image_center", True))

    objective_name = str(params.get("objective_name") or "").strip().lower()
    if entrypoint is None and objective_name != "4x":
        raise ValueError("默认 4x 定位模型入口只允许 objective_name='4x'")

    fov_cfg = scan_result.get("scan_config", {}).get("fov_mm", {}) or {}
    fov_w_mm = float(fov_cfg.get("width"))
    fov_h_mm = float(fov_cfg.get("height"))

    images: List[Dict[str, Any]] = []
    captures = list(scan_result.get("captures", []) or [])
    total_captures = max(1, len(captures))
    report_progress(params, "detect", 0, params.get("well_name"), "detect started")

    for image_index, capture in enumerate(captures, start=1):
        raise_if_cancel_requested(params, f"before_detect:image_{capture.get('index')}")
        report_progress(
            params,
            "detect",
            (image_index - 1) * 100 / total_captures,
            params.get("well_name"),
            f"detecting image {image_index}/{total_captures}",
        )
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
        overlay_dir = None
        vision_output_dir = None
        detect_kwargs: Dict[str, Any] = {}
        # Keep this rule-only output option out of model and third-party calls.
        if isinstance(entrypoint, str) and entrypoint in _RULE_PATH_ENTRYPOINTS:
            detect_kwargs["save_debug"] = save_debug
        if save_overlay:
            overlay_dir = _choose_overlay_dir(params, image_path)
            if overlay_source == "vision":
                vision_output_dir = _vision_output_dir_for_image(image_path, overlay_dir)
                detect_kwargs["out_dir"] = str(vision_output_dir)
                scale_bar = _build_scale_bar_config(detect_cfg, mm_per_pixel)
                if scale_bar is not None:
                    detect_kwargs["scale_bar"] = scale_bar

        detect_result = run_detect_on_image(
            image_path,
            entrypoint=entrypoint,
            detect_kwargs={
                **detect_kwargs,
                "model_dir": detect_cfg.get("model_dir") or params.get("detect_model_dir"),
                "provider": str(detect_cfg.get("provider") or params.get("detect_provider") or "cuda"),
                "allow_cpu_fallback": bool(
                    detect_cfg.get("allow_cpu_fallback", params.get("detect_allow_cpu_fallback", False))
                ),
                "objective_name": params.get("objective_name"),
                "mm_per_pixel": mm_per_pixel,
                "well_border_margin_mm": float(detect_cfg.get("well_border_margin_mm", 0.0) or 0.0),
                "well_border_margin_px": float(detect_cfg.get("well_border_margin_px", 30.0) or 30.0),
                "detect_well_border": bool(detect_cfg.get("detect_well_border", True)),
            },
        )

        def build_items(raw_items: List[Dict[str, Any]], *, review: bool) -> List[Dict[str, Any]]:
            built: List[Dict[str, Any]] = []
            for clone in raw_items:
                center_px = clone["center_px"]
                offset_px = _offset_from_center(center_px, image_center)
                polygon = _extract_polygon_points(clone.get("raw") or {})
                source_id = f"I{int(capture['index'])}:{clone['clone_id']}"
                is_v2 = int(detect_result.get("schema_version", 1)) >= 2
                explicit_pickable = clone.get("is_pickable")
                if review or is_v2:
                    is_pickable = explicit_pickable is True
                else:
                    is_pickable = explicit_pickable if explicit_pickable is not None else clone.get("is_valid_for_compensation") is not False

                built.append(
                    {
                        "clone_id": clone["clone_id"],
                        "source_detection_id": source_id,
                        "center_px": center_px,
                        "offset_from_image_center_px": offset_px,
                        "bbox": clone.get("bbox"),
                        "area_px": clone.get("area_px"),
                        "score": clone.get("score"),
                        "confidence": clone.get("confidence"),
                        "detection_confidence": clone.get("detection_confidence"),
                        "segmentation_status": clone.get("segmentation_status"),
                        "segmentation_score": clone.get("segmentation_score"),
                        "status": clone.get("status") or ("review" if review else "accepted"),
                        "instance_label": clone.get("instance_label"),
                        "detection_source": clone.get("detection_source"),
                        "review_reasons": list(clone.get("review_reasons") or []),
                        "quality_assessment": clone.get("quality_assessment"),
                        "location_valid": clone.get("location_valid"),
                        "eligible_for_10x_centering": clone.get("eligible_for_10x_centering"),
                        "is_valid_for_compensation": clone.get("is_valid_for_compensation"),
                        "source_image_path": image_path,
                        "stage_x_actual": actual_x,
                        "stage_y_actual": actual_y,
                        "has_polygon": bool(polygon),
                        "contour_points": [list(point) for point in polygon] if polygon else None,
                        "touch_image_border": clone.get("touch_image_border"),
                        "image_border_sides": list(clone.get("image_border_sides") or []),
                        "image_edge_clipped": clone.get("image_edge_clipped"),
                        "truncated": clone.get("truncated"),
                        "well_border_detected": clone.get("well_border_detected"),
                        "near_well_border": clone.get("near_well_border"),
                        "distance_to_well_edge_px": clone.get("distance_to_well_edge_px"),
                        "distance_to_well_edge_mm": clone.get("distance_to_well_edge_mm"),
                        "is_pickable": bool(is_pickable),
                    }
                )
            return built

        raw_clones = detect_result.get("clones", []) or []
        raw_reviews = detect_result.get("review_candidates", []) or []
        clones = build_items(raw_clones, review=False)
        review_candidates = build_items(raw_reviews, review=True)

        overlay_image_path = None
        if save_overlay:
            vision_overlay = _vision_overlay_path(vision_output_dir) if vision_output_dir else None
            if vision_overlay and vision_overlay.exists():
                overlay_image_path = str(vision_overlay)
            else:
                overlay_path = _overlay_path_for_image(image_path, overlay_dir or _choose_overlay_dir(params, image_path))
                overlay_image_path = _render_overlay_image(
                    image_path=image_path,
                    image_center_px=image_center,
                    raw_clones=raw_clones,
                    overlay_path=overlay_path,
                    draw_bbox=draw_bbox,
                    draw_center=draw_center,
                    draw_image_center=draw_image_center,
                )

        images.append(
            {
                "index": int(capture["index"]),
                "row_index": int(capture["row_index"]),
                "col_index": int(capture["col_index"]),
                "image_path": image_path,
                "overlay_image_path": overlay_image_path,
                "stage_x_target": int(capture["stage_x_target"]),
                "stage_y_target": int(capture["stage_y_target"]),
                "stage_x_actual": actual_x,
                "stage_y_actual": actual_y,
                "image_width_px": width,
                "image_height_px": height,
                "image_center_px": image_center,
                "mm_per_pixel": mm_per_pixel,
                "schema_version": int(detect_result.get("schema_version", 1)),
                "clone_count": len(clones),
                "review_candidate_count": len(review_candidates),
                "clones": clones,
                "review_candidates": review_candidates,
                "models": detect_result.get("models") or {},
                "runtime": detect_result.get("runtime") or {},
            }
        )
        report_progress(
            params,
            "detect",
            image_index * 100 / total_captures,
            params.get("well_name"),
            f"detected image {image_index}/{total_captures}",
        )
        raise_if_cancel_requested(params, f"after_detect:image_{capture.get('index')}")

    total_image_clones = sum(len(x["clones"]) for x in images)
    total_review_candidates = sum(len(x["review_candidates"]) for x in images)
    dedupe_cfg = detect_cfg.get("deduplication") or {}
    if total_image_clones == 0:
        deduped = {
            "unique_clones": [],
            "metadata": {
                "method": "physical_bbox_iom_and_center_distance",
                "input_observation_count": 0,
                "merged_observation_count": 0,
                "iom_threshold": float(dedupe_cfg.get("intersection_over_min_threshold", 0.50)),
            },
        }
    else:
        dedupe_scan_config = dict(scan_result.get("scan_config") or {})
        dedupe_scan_config["dedupe_registration_calibrated"] = bool(
            dedupe_cfg.get("calibrated", False)
        )
        deduped = dedupe_well_clones(
            images,
            plate_cfg=ctx.get("plate") or {},
            reference=scan_result.get("reference") or {},
            scan_config=dedupe_scan_config,
            iom_threshold=float(dedupe_cfg.get("intersection_over_min_threshold", 0.50)),
            registration_tolerance_mm=float(dedupe_cfg.get("registration_tolerance_mm", 0.10)),
        )
    unique_clones = deduped["unique_clones"]
    report_progress(params, "detect", 100, params.get("well_name"), "detect completed")

    result = {
        "schema_version": 2,
        "task_id": params["task_id"],
        "status": "success",
        "task_type": "single_well_scan_and_detect",
        "plate_type": params["plate_type"],
        "well_name": params["well_name"],
        "objective_name": params["objective_name"],
        "reference": scan_result.get("reference"),
        "scan_config": scan_result.get("scan_config"),
        "scan_result_json": params.get("scan_result_json"),
        "detect_overlay_dir": str(_choose_overlay_dir(params, images[0]["image_path"])) if images and save_overlay else None,
        "image_count": len(images),
        "total_image_clone_count": total_image_clones,
        "review_candidate_count": total_review_candidates,
        "unique_clone_count": len(unique_clones),
        "total_clone_count": len(unique_clones),
        "unique_clones": unique_clones,
        "deduplication": deduped["metadata"],
        "quality_assessment": {
            "status": "not_assessed",
            "reason": "4x is localization-only; colony quality requires 10x observation",
        },
        "images": images,
    }

    output_json = params.get("detect_output_json")
    if output_json:
        atomic_write_json(output_json, result)

    return result
