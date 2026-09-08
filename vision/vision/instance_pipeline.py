"""Model-based 4x iPSC colony localization and instance segmentation.

This is deliberately separate from ``detect_pipeline`` (the legacy heuristic
backend).  The detector is run on the whole image and deterministic overlapping
tiles; each retained box is then segmented in its own padded ROI.  Only a
successful, high-confidence instance enters ``components``.  Lower-confidence
or failed-segmentation detections are retained in ``review_candidates`` and are
never counted as formal colonies.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, Iterable, Sequence

import cv2
import numpy as np

from workflow.file_io import atomic_write_json

from .image_loader import load_image, save_image, to_gray_u8
from .instance_postprocess import (
    build_sliding_tiles,
    component_containing_or_nearest_center,
    mask_geometry,
    merge_detections,
)
from .model_runtime import (
    OnnxModelSpec,
    VisionModelBundle,
    VisionModelError,
    VisionInputQualityError,
    load_model_bundle,
)
from .postprocess import draw_scale_bar


RESULT_SCHEMA_VERSION = 2


def _sigmoid(values: np.ndarray) -> np.ndarray:
    values = np.clip(values.astype(np.float32, copy=False), -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-values))


def _evaluate_image_qc(gray: np.ndarray, config: Dict[str, Any]) -> Dict[str, Any]:
    enabled = bool(config.get("enabled"))
    if not enabled:
        return {
            "status": "disabled",
            "disabled_reason": str(config.get("disabled_reason") or ""),
        }
    height, width = gray.shape
    scale = min(1.0, 1024.0 / max(height, width))
    work = (
        cv2.resize(gray, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else gray
    )
    p01, p99 = [float(value) for value in np.percentile(work, [1.0, 99.0])]
    metrics = {
        "p01": p01,
        "p99": p99,
        "dynamic_range_p01_p99": p99 - p01,
        "dark_fraction": float(np.mean(work <= int(config.get("dark_value", 1)))),
        "bright_fraction": float(np.mean(work >= int(config.get("bright_value", 254)))),
        "laplacian_variance": float(cv2.Laplacian(work, cv2.CV_32F).var()),
    }
    failures: list[str] = []
    if metrics["dynamic_range_p01_p99"] < float(config["min_dynamic_range"]):
        failures.append("insufficient_dynamic_range")
    if metrics["dark_fraction"] > float(config["max_dark_fraction"]):
        failures.append("excessive_dark_saturation")
    if metrics["bright_fraction"] > float(config["max_bright_fraction"]):
        failures.append("excessive_bright_saturation")
    if metrics["laplacian_variance"] < float(config["min_laplacian_variance"]):
        failures.append("insufficient_focus_or_texture")
    return {
        "status": "pass" if not failures else "abstain",
        "failures": failures,
        "metrics": metrics,
    }


def _source_for_spec(src: np.ndarray, spec: OnnxModelSpec) -> np.ndarray:
    gray = to_gray_u8(src)
    if spec.color_mode == "gray":
        return gray[:, :, None]
    if spec.color_mode == "gray_replicated":
        return np.repeat(gray[:, :, None], 3, axis=2)

    if src.ndim == 2:
        bgr = cv2.cvtColor(to_gray_u8(src), cv2.COLOR_GRAY2BGR)
    elif src.shape[2] == 4:
        bgr = src[:, :, :3]
    else:
        bgr = src[:, :, :3]
    if bgr.dtype != np.uint8:
        channels = [to_gray_u8(bgr[:, :, index]) for index in range(3)]
        bgr = np.stack(channels, axis=2)
    return bgr[:, :, ::-1] if spec.color_mode == "rgb" else bgr


def _normalize_chw(image: np.ndarray, spec: OnnxModelSpec) -> np.ndarray:
    tensor = image.astype(np.float32)
    mean = np.asarray(spec.mean, dtype=np.float32).reshape(1, 1, -1)
    std = np.asarray(spec.std, dtype=np.float32).reshape(1, 1, -1)
    tensor = (tensor - mean) / std
    return np.ascontiguousarray(tensor.transpose(2, 0, 1)[None, ...], dtype=np.float32)


def _cv_interpolation(spec: OnnxModelSpec) -> int:
    return cv2.INTER_AREA if spec.resize_interpolation == "area" else cv2.INTER_LINEAR


def _letterbox_tensor(src: np.ndarray, spec: OnnxModelSpec, pad_value: int = 114) -> tuple[np.ndarray, Dict[str, float]]:
    prepared = _source_for_spec(src, spec)
    src_h, src_w = prepared.shape[:2]
    dst_w, dst_h = spec.input_size
    scale = min(dst_w / float(src_w), dst_h / float(src_h))
    resized_w = max(1, min(dst_w, int(round(src_w * scale))))
    resized_h = max(1, min(dst_h, int(round(src_h * scale))))
    resized = cv2.resize(prepared, (resized_w, resized_h), interpolation=_cv_interpolation(spec))
    if resized.ndim == 2:
        resized = resized[:, :, None]
    canvas = np.full((dst_h, dst_w, spec.input_channels), int(pad_value), dtype=np.uint8)
    if spec.pad_alignment == "top_left":
        pad_x = pad_y = 0
    else:
        pad_x = (dst_w - resized_w) // 2
        pad_y = (dst_h - resized_h) // 2
    canvas[pad_y : pad_y + resized_h, pad_x : pad_x + resized_w] = resized
    return _normalize_chw(canvas, spec), {
        "scale": float(scale),
        "pad_x": float(pad_x),
        "pad_y": float(pad_y),
        "source_width": float(src_w),
        "source_height": float(src_h),
    }


def _stretch_tensor(src: np.ndarray, spec: OnnxModelSpec) -> np.ndarray:
    prepared = _source_for_spec(src, spec)
    dst_w, dst_h = spec.input_size
    resized = cv2.resize(prepared, (dst_w, dst_h), interpolation=_cv_interpolation(spec))
    if resized.ndim == 2:
        resized = resized[:, :, None]
    return _normalize_chw(resized, spec)


def _as_detector_rows(raw: Any) -> np.ndarray:
    rows = np.asarray(raw, dtype=np.float32)
    if rows.ndim == 3 and rows.shape[0] == 1:
        rows = rows[0]
    if rows.size == 0:
        return np.empty((0, 6), dtype=np.float32)
    if rows.ndim != 2 or rows.shape[1] != 6:
        raise VisionModelError(f"detector returned shape {list(rows.shape)}; expected [N,6]")
    return rows


def _run_detector_view(
    src: np.ndarray,
    bundle: VisionModelBundle,
    *,
    source: str,
    global_x: int = 0,
    global_y: int = 0,
) -> list[Dict[str, Any]]:
    cfg = bundle.manifest.inference
    tensor, transform = _letterbox_tensor(
        src,
        bundle.manifest.detector,
        pad_value=int(cfg.get("letterbox_value", 114)),
    )
    rows = _as_detector_rows(bundle.detector.run(tensor)[0])
    threshold = float(cfg.get("detector_review_threshold", 0.35))
    width, height = int(transform["source_width"]), int(transform["source_height"])
    scale, pad_x, pad_y = transform["scale"], transform["pad_x"], transform["pad_y"]
    detections: list[Dict[str, Any]] = []
    for row in rows:
        x1, y1, x2, y2, score, class_id = [float(value) for value in row]
        if not np.isfinite(row).all() or score < threshold or int(round(class_id)) != 0:
            continue
        x1 = float(np.clip((x1 - pad_x) / scale, 0, width))
        y1 = float(np.clip((y1 - pad_y) / scale, 0, height))
        x2 = float(np.clip((x2 - pad_x) / scale, 0, width))
        y2 = float(np.clip((y2 - pad_y) / scale, 0, height))
        if x2 - x1 < 2.0 or y2 - y1 < 2.0:
            continue
        detections.append(
            {
                "bbox": [x1 + global_x, y1 + global_y, x2 - x1, y2 - y1],
                "score": float(np.clip(score, 0.0, 1.0)),
                "class_id": 0,
                "source": source,
            }
        )
    return detections


def _detect_candidates(
    src: np.ndarray, bundle: VisionModelBundle
) -> tuple[list[Dict[str, Any]], int, int]:
    cfg = bundle.manifest.inference
    height, width = src.shape[:2]
    detections = _run_detector_view(src, bundle, source="full_image")
    tile_count = 0
    if bool(cfg.get("tile_enabled", True)):
        tile_size = int(cfg.get("tile_size", 1280))
        overlap = float(cfg.get("tile_overlap", 0.20))
        tiles = build_sliding_tiles(width, height, tile_size=tile_size, overlap=overlap)
        tile_count = len(tiles)
        for tile_index, tile in enumerate(tiles, start=1):
            crop = src[tile.y : tile.y + tile.height, tile.x : tile.x + tile.width]
            local = _run_detector_view(
                crop,
                bundle,
                source=f"tile_{tile_index:03d}",
                global_x=tile.x,
                global_y=tile.y,
            )
            # Keep every tile detection.  A colony can be detected only in a
            # neighbouring tile where more of it is visible; centre-ownership
            # filtering here would turn that valid sole detection into a miss.
            # Global box fusion and the later mask-level duplicate check remove
            # repeated observations deterministically.
            detections.extend(local)

    raw_count = len(detections)
    merged = merge_detections(
        detections,
        iou_threshold=float(cfg.get("detector_merge_iou", 0.90)),
        containment_threshold=float(cfg.get("detector_merge_containment", 0.95)),
        center_ratio=float(cfg.get("detector_merge_center_ratio", 0.10)),
    )
    return merged, tile_count, raw_count


def _probability_map(raw: Any) -> np.ndarray:
    values = np.asarray(raw, dtype=np.float32)
    if values.ndim == 4 and values.shape[:2] == (1, 1):
        values = values[0, 0]
    elif values.ndim == 3 and values.shape[0] == 1:
        values = values[0]
    if values.ndim != 2:
        raise VisionModelError(f"segmenter returned shape {list(values.shape)}; expected [1,1,H,W]")
    if not np.isfinite(values).all():
        raise VisionModelError("segmenter returned NaN or infinite values")
    return _sigmoid(values)


def _border_sides(bbox: Sequence[int], image_width: int, image_height: int, tolerance: int = 1) -> list[str]:
    x, y, width, height = [int(v) for v in bbox[:4]]
    sides: list[str] = []
    if x <= tolerance:
        sides.append("left")
    if y <= tolerance:
        sides.append("top")
    if x + width >= image_width - tolerance:
        sides.append("right")
    if y + height >= image_height - tolerance:
        sides.append("bottom")
    return sides


def _segment_detection(
    src: np.ndarray,
    detection: Dict[str, Any],
    bundle: VisionModelBundle,
) -> Dict[str, Any]:
    cfg = bundle.manifest.inference
    image_h, image_w = src.shape[:2]
    x, y, width, height = [float(v) for v in detection["bbox"]]
    pad_ratio = float(cfg.get("roi_pad_ratio", 0.20))
    pad_x, pad_y = width * pad_ratio, height * pad_ratio
    x0 = max(0, int(np.floor(x - pad_x)))
    y0 = max(0, int(np.floor(y - pad_y)))
    x1 = min(image_w, int(np.ceil(x + width + pad_x)))
    y1 = min(image_h, int(np.ceil(y + height + pad_y)))
    review_reasons: list[str] = []
    base: Dict[str, Any] = {
        "detector_bbox": [int(round(x)), int(round(y)), int(round(width)), int(round(height))],
        "detection_confidence": float(detection["score"]),
        "confidence": float(detection["score"]),
        "detection_source": str(detection.get("source") or "model"),
        "class_name": "ipsc_clone",
        "quality_assessment": {
            "status": "not_assessed",
            "reason": "4x images are used for localization only; quality requires 10x review",
        },
        "is_valid_for_compensation": False,
        "is_pickable": False,
        "eligible_for_10x_centering": False,
        "location_valid": False,
    }
    if x1 <= x0 or y1 <= y0:
        return {**base, "bbox": base["detector_bbox"], "segmentation_status": "failed", "review_reasons": ["empty_roi"]}

    roi = src[y0:y1, x0:x1]
    try:
        tensor = _stretch_tensor(roi, bundle.manifest.segmenter)
        outputs = bundle.segmenter.run(tensor)
        foreground = _probability_map(outputs[0])
        mask = foreground >= float(cfg.get("segment_threshold", 0.50))
        if bundle.manifest.segmenter.output_format == "foreground_boundary_logits":
            boundary = _probability_map(outputs[1])
            mask &= boundary < float(cfg.get("boundary_threshold", 0.50))
    except Exception as exc:
        raise VisionModelError(f"segmenter inference failed for ROI {(x0, y0, x1, y1)}: {exc}") from exc

    roi_h, roi_w = roi.shape[:2]
    mask_u8 = cv2.resize(mask.astype(np.uint8) * 255, (roi_w, roi_h), interpolation=cv2.INTER_NEAREST)
    probability = cv2.resize(foreground, (roi_w, roi_h), interpolation=cv2.INTER_LINEAR)
    morphology_px = int(cfg.get("mask_morphology_px", 3))
    if morphology_px > 1:
        size = morphology_px if morphology_px % 2 == 1 else morphology_px + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
        mask_u8 = cv2.morphologyEx(mask_u8, cv2.MORPH_OPEN, kernel)
        mask_u8 = cv2.morphologyEx(mask_u8, cv2.MORPH_CLOSE, kernel)

    detector_center_local = [x + width / 2.0 - x0, y + height / 2.0 - y0]
    mask_u8 = component_containing_or_nearest_center(
        mask_u8,
        detector_center_local,
        target_bbox_xywh=[x - x0, y - y0, width, height],
        min_component_area_px=int(cfg.get("min_connected_component_area_px", 4)),
    )
    geometry = mask_geometry(mask_u8, offset_x=x0, offset_y=y0)
    if geometry is None:
        return {
            **base,
            "bbox": base["detector_bbox"],
            "segmentation_status": "failed",
            "segmentation_score": None,
            "review_reasons": ["empty_mask"],
        }

    min_area_px = int(cfg.get("min_mask_area_px", 64))
    if int(geometry["area_px"]) < min_area_px:
        review_reasons.append("mask_area_below_minimum")
    selected = mask_u8 > 0
    inside_score = float(np.mean(probability[selected])) if np.any(selected) else 0.0
    outside = np.logical_not(selected)
    outside_score = float(np.mean(probability[outside])) if np.any(outside) else 0.0
    segmentation_score = float(np.clip(inside_score - outside_score, 0.0, 1.0))
    if segmentation_score < float(cfg.get("segment_review_score", 0.50)):
        review_reasons.append("low_segmentation_score")
    detector_local = np.zeros_like(mask_u8, dtype=np.uint8)
    box_x0 = max(0, int(np.floor(x)) - x0)
    box_y0 = max(0, int(np.floor(y)) - y0)
    box_x1 = min(roi_w, int(np.ceil(x + width)) - x0)
    box_y1 = min(roi_h, int(np.ceil(y + height)) - y0)
    detector_local[box_y0:box_y1, box_x0:box_x1] = 1
    mask_area = int(np.count_nonzero(selected))
    intersection = int(np.count_nonzero(np.logical_and(selected, detector_local > 0)))
    if mask_area <= 0 or intersection / float(mask_area) < float(cfg.get("min_mask_inside_detector_ratio", 0.50)):
        review_reasons.append("mask_detector_inconsistent")
    roi_boundary_sides = []
    if np.any(selected[:, 0]):
        roi_boundary_sides.append("left")
    if np.any(selected[:, -1]):
        roi_boundary_sides.append("right")
    if np.any(selected[0, :]):
        roi_boundary_sides.append("top")
    if np.any(selected[-1, :]):
        roi_boundary_sides.append("bottom")
    mask_touches_roi = bool(roi_boundary_sides)
    internal_roi_clip_sides = [
        side
        for side in roi_boundary_sides
        if (side == "left" and x0 > 0)
        or (side == "right" and x1 < image_w)
        or (side == "top" and y0 > 0)
        or (side == "bottom" and y1 < image_h)
    ]
    if internal_roi_clip_sides:
        review_reasons.append("mask_touches_roi_boundary")
    sides = _border_sides(geometry["bbox"], image_w, image_h)
    return {
        **base,
        **geometry,
        "segmentation_status": "success" if not review_reasons else "review",
        "segmentation_score": segmentation_score,
        "touch_image_border": bool(sides),
        "image_border_sides": sides,
        "image_edge_clipped": bool(sides),
        "truncated": bool(sides),
        "mask_touches_roi_boundary": mask_touches_roi,
        "roi_boundary_sides": roi_boundary_sides,
        "internal_roi_clip_sides": internal_roi_clip_sides,
        "review_reasons": review_reasons,
        "location_valid": not review_reasons,
        "eligible_for_10x_centering": not review_reasons,
        "_roi": [x0, y0, x1, y1],
        "_mask": mask_u8,
    }


def _stable_sort(items: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            int((item.get("center_pixel") or [0, 0])[1]),
            int((item.get("center_pixel") or [0, 0])[0]),
            -float(item.get("detection_confidence") or 0.0),
        ),
    )


def _build_review_geometry(item: Dict[str, Any], image_width: int, image_height: int) -> None:
    if item.get("center_pixel") is not None:
        return
    x, y, width, height = [int(round(float(value))) for value in item["bbox"]]
    x = max(0, min(image_width - 1, x))
    y = max(0, min(image_height - 1, y))
    width = max(1, min(image_width - x, width))
    height = max(1, min(image_height - y, height))
    item["bbox"] = [x, y, width, height]
    item["center_pixel"] = [x + width // 2, y + height // 2]
    item["safe_point"] = list(item["center_pixel"])
    item["area_px"] = None
    item["contour_points"] = []
    sides = _border_sides(item["bbox"], image_width, image_height)
    item["touch_image_border"] = bool(sides)
    item["image_border_sides"] = sides
    item["image_edge_clipped"] = bool(sides)
    item["truncated"] = bool(sides)


def _instance_mask_overlap(a: Dict[str, Any], b: Dict[str, Any]) -> tuple[float, float]:
    ax0, ay0, ax1, ay1 = a["_roi"]
    bx0, by0, bx1, by1 = b["_roi"]
    x0, y0 = max(ax0, bx0), max(ay0, by0)
    x1, y1 = min(ax1, bx1), min(ay1, by1)
    if x1 <= x0 or y1 <= y0:
        return 0.0, 0.0
    a_mask = a["_mask"] > 0
    b_mask = b["_mask"] > 0
    a_overlap = a_mask[y0 - ay0 : y1 - ay0, x0 - ax0 : x1 - ax0]
    b_overlap = b_mask[y0 - by0 : y1 - by0, x0 - bx0 : x1 - bx0]
    intersection = int(np.count_nonzero(np.logical_and(a_overlap, b_overlap)))
    a_area, b_area = int(np.count_nonzero(a_mask)), int(np.count_nonzero(b_mask))
    minimum = min(a_area, b_area)
    union = a_area + b_area - intersection
    return (
        intersection / float(minimum) if minimum > 0 else 0.0,
        intersection / float(union) if union > 0 else 0.0,
    )


def _bbox_iou(a: Sequence[float], b: Sequence[float]) -> float:
    ax, ay, aw, ah = [float(value) for value in a[:4]]
    bx, by, bw, bh = [float(value) for value in b[:4]]
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = aw * ah + bw * bh - intersection
    return intersection / union if union > 0 else 0.0


def _duplicate_instances(
    a: Dict[str, Any], b: Dict[str, Any], *, mask_iom_threshold: float
) -> bool:
    mask_iom, mask_iou = _instance_mask_overlap(a, b)
    if mask_iom < mask_iom_threshold:
        return False
    detector_iou = _bbox_iou(a["detector_bbox"], b["detector_bbox"])
    acx, acy = a["center_pixel"]
    bcx, bcy = b["center_pixel"]
    smaller_diag = min(
        float(np.hypot(a["bbox"][2], a["bbox"][3])),
        float(np.hypot(b["bbox"][2], b["bbox"][3])),
    )
    close_center = float(np.hypot(acx - bcx, acy - bcy)) <= 0.10 * max(1.0, smaller_diag)
    return mask_iou >= 0.75 or (detector_iou >= 0.85 and close_center)


def _suppress_duplicate_instance_masks(
    components: list[Dict[str, Any]], *, threshold: float
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    kept: list[Dict[str, Any]] = []
    duplicates: list[Dict[str, Any]] = []
    for item in sorted(
        components,
        key=lambda value: (-float(value["detection_confidence"]), value["center_pixel"][1], value["center_pixel"][0]),
    ):
        if any(_duplicate_instances(item, existing, mask_iom_threshold=threshold) for existing in kept):
            item["status"] = "review"
            item["segmentation_status"] = "review"
            item["location_valid"] = False
            item["review_reasons"] = sorted(
                set(list(item.get("review_reasons") or []) + ["duplicate_instance_mask"])
            )
            duplicates.append(item)
        else:
            kept.append(item)
    return kept, duplicates


def _materialize_instance_mask(
    components: list[Dict[str, Any]], image_shape: Sequence[int]
) -> np.ndarray:
    labels = np.zeros((int(image_shape[0]), int(image_shape[1])), dtype=np.uint16)
    if len(components) > np.iinfo(np.uint16).max:
        raise VisionModelError("instance count exceeds uint16 label capacity")
    for component in sorted(components, key=lambda item: -float(item["detection_confidence"])):
        x0, y0, x1, y1 = component["_roi"]
        region = labels[y0:y1, x0:x1]
        mask = component["_mask"] > 0
        region[np.logical_and(mask, region == 0)] = int(component["instance_label"])
    return labels


def _reconcile_component_geometry_with_labels(
    components: list[Dict[str, Any]], labels: np.ndarray
) -> None:
    """Make JSON geometry describe exactly the pixels written to the label PNG."""
    for component in components:
        x0, y0, x1, y1 = component["_roi"]
        local = (labels[y0:y1, x0:x1] == int(component["instance_label"])).astype(np.uint8) * 255
        geometry = mask_geometry(local, offset_x=x0, offset_y=y0)
        if geometry is None:
            raise VisionModelError(
                f"instance {component.get('id') or component['instance_label']} lost all pixels during label arbitration"
            )
        component.update(geometry)


def _clean_internal_fields(items: Iterable[Dict[str, Any]]) -> None:
    for item in items:
        item.pop("_roi", None)
        item.pop("_mask", None)


def _save_outputs(
    src_path: str,
    out_dir: str | Path,
    src: np.ndarray,
    gray: np.ndarray,
    labels: np.ndarray,
    components: list[Dict[str, Any]],
    reviews: list[Dict[str, Any]],
    result: Dict[str, Any],
    scale_bar: Dict[str, Any] | None,
) -> None:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    save_image(out_path / "01_gray.bmp", gray)
    save_image(out_path / "05_instance_mask.png", labels)
    save_image(out_path / "05_contour_mask.bmp", (labels > 0).astype(np.uint8) * 255)

    if src.ndim == 2:
        overlay = cv2.cvtColor(to_gray_u8(src), cv2.COLOR_GRAY2BGR)
    else:
        overlay = src[:, :, :3].copy()
        if overlay.dtype != np.uint8:
            overlay = np.stack(
                [to_gray_u8(overlay[:, :, channel]) for channel in range(3)], axis=2
            )
    for item in components:
        contour = np.asarray(item.get("contour_points") or [], dtype=np.int32)
        if contour.size:
            cv2.polylines(overlay, [contour.reshape(-1, 1, 2)], True, (0, 255, 0), 6, cv2.LINE_AA)
        cx, cy = item["center_pixel"]
        cv2.drawMarker(overlay, (int(cx), int(cy)), (0, 0, 255), cv2.MARKER_CROSS, 28, 4)
        cv2.putText(overlay, item["id"], (int(cx) + 12, int(cy) - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2, cv2.LINE_AA)
    for item in reviews:
        x, y, width, height = item["bbox"]
        cv2.rectangle(overlay, (x, y), (x + width, y + height), (0, 165, 255), 3)
        cv2.putText(overlay, item["id"], (x + 5, max(20, y - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 165, 255), 2, cv2.LINE_AA)
    result["scale_bar"] = draw_scale_bar(overlay, scale_bar)
    save_image(out_path / "06_overlay.bmp", overlay)
    atomic_write_json(out_path / "07_result.json", result)


@lru_cache(maxsize=1)
def _cached_bundle(model_dir: str, provider: str, allow_cpu_fallback: bool) -> VisionModelBundle:
    provider = provider.strip().lower()
    if provider not in {"cuda", "cpu", "auto"}:
        raise VisionModelError("provider must be 'cuda', 'cpu', or 'auto'")
    prefer_cuda = provider != "cpu"
    fallback = bool(allow_cpu_fallback or provider == "auto")
    return load_model_bundle(
        model_dir,
        prefer_cuda=prefer_cuda,
        allow_cpu_fallback=fallback,
    )


def detect_from_array(
    src: np.ndarray,
    *,
    model_dir: str | Path,
    src_path: str = "in_memory",
    out_dir: str | Path | None = None,
    provider: str = "cuda",
    allow_cpu_fallback: bool = False,
    objective_name: str = "4x",
    scale_bar: Dict[str, Any] | None = None,
    model_bundle: VisionModelBundle | None = None,
    **compatibility_kwargs: Any,
) -> Dict[str, Any]:
    """Run v2 model inference on an in-memory image."""
    if str(objective_name).strip().lower() != "4x":
        raise VisionModelError(
            f"the localization model only accepts objective_name='4x', got {objective_name!r}"
        )
    if src is None or src.ndim not in (2, 3):
        raise ValueError("src must be a gray or color image array")
    allowed_compatibility = {
        "mm_per_pixel",
    }
    unknown = sorted(set(compatibility_kwargs) - allowed_compatibility)
    if unknown:
        raise VisionModelError(f"unsupported model pipeline arguments: {unknown}")

    started = perf_counter()
    bundle = model_bundle or _cached_bundle(
        str(Path(model_dir).resolve(strict=False)), provider, bool(allow_cpu_fallback)
    )
    load_ms = (perf_counter() - started) * 1000.0
    image_h, image_w = src.shape[:2]
    expected_w, expected_h = bundle.manifest.expected_image_size
    if (image_w, image_h) != (expected_w, expected_h):
        raise VisionModelError(
            f"input size mismatch: expected {expected_w}x{expected_h}, got {image_w}x{image_h}"
        )
    gray = to_gray_u8(src)
    image_qc = _evaluate_image_qc(gray, bundle.manifest.image_qc)
    if image_qc["status"] == "abstain":
        raise VisionInputQualityError(
            "image QC failed; no clone count was produced: " + ", ".join(image_qc["failures"])
        )

    detection_started = perf_counter()
    detections, tile_count, raw_detection_count = _detect_candidates(src, bundle)
    detection_ms = (perf_counter() - detection_started) * 1000.0

    segmentation_started = perf_counter()
    segmented = [_segment_detection(src, item, bundle) for item in detections]
    accept_threshold = float(bundle.manifest.inference.get("detector_accept_threshold", 0.70))
    components: list[Dict[str, Any]] = []
    reviews: list[Dict[str, Any]] = []
    for item in segmented:
        reasons = list(item.get("review_reasons") or [])
        if float(item["detection_confidence"]) < accept_threshold:
            reasons.append("detector_score_below_accept_threshold")
        if item.get("segmentation_status") != "success" or reasons:
            item["status"] = "review"
            item["review_reasons"] = sorted(set(reasons))
            reviews.append(item)
        else:
            item["status"] = "accepted"
            item["review_reasons"] = []
            components.append(item)
    for item in reviews:
        _build_review_geometry(item, image_w, image_h)
    components, duplicate_reviews = _suppress_duplicate_instance_masks(
        components,
        threshold=float(bundle.manifest.inference.get("mask_duplicate_iom", 0.85)),
    )
    reviews.extend(duplicate_reviews)
    components = _stable_sort(components)
    reviews = _stable_sort(reviews)
    for index, item in enumerate(components, start=1):
        item["instance_label"] = index
    labels = _materialize_instance_mask(components, src.shape)
    _reconcile_component_geometry_with_labels(components, labels)
    components = _stable_sort(components)
    for index, item in enumerate(components, start=1):
        item["id"] = f"C{index:03d}"
        item["instance_label"] = index
    labels = _materialize_instance_mask(components, src.shape)
    _reconcile_component_geometry_with_labels(components, labels)
    for index, item in enumerate(reviews, start=1):
        item["id"] = f"R{index:03d}"
        item["instance_label"] = 0
    segmentation_ms = (perf_counter() - segmentation_started) * 1000.0
    _clean_internal_fields(components)
    _clean_internal_fields(reviews)

    result: Dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "objective_name": "4x",
        "purpose": "localization_only",
        "quality_assessment": {
            "status": "not_assessed",
            "required_objective": "10x",
        },
        "image_qc": image_qc,
        "well_region_policy": "not_used_to_suppress_detections",
        "input_path": str(src_path),
        "input_size": {"width": image_w, "height": image_h},
        "component_count": len(components),
        "review_candidate_count": len(reviews),
        "component_ids": [item["id"] for item in components],
        "review_candidate_ids": [item["id"] for item in reviews],
        "components": components,
        "review_candidates": reviews,
        "models": {
            "manifest_path": str(bundle.manifest.path),
            "manifest_sha256": bundle.manifest.manifest_sha256,
            "model_version": bundle.manifest.model_version,
            "dataset_version": bundle.manifest.dataset_version,
            "detector_sha256": bundle.manifest.detector.expected_sha256,
            "segmenter_sha256": bundle.manifest.segmenter.expected_sha256,
            "effective_inference_config": dict(bundle.manifest.inference),
            "effective_image_qc_config": dict(bundle.manifest.image_qc),
        },
        "runtime": {
            "backend": bundle.runtime_backend,
            "fallback_reasons": bundle.fallback_reasons,
            "tile_count": tile_count,
            "raw_detection_count": raw_detection_count,
            "fused_detection_count": len(detections),
            "model_load_ms": round(load_ms, 3),
            "detection_ms": round(detection_ms, 3),
            "segmentation_ms": round(segmentation_ms, 3),
        },
        "scale_bar": None,
    }
    if out_dir is not None:
        save_started = perf_counter()
        _save_outputs(src_path, out_dir, src, gray, labels, components, reviews, result, scale_bar)
        result["runtime"]["output_save_ms"] = round((perf_counter() - save_started) * 1000.0, 3)
        result["runtime"]["total_ms"] = round((perf_counter() - started) * 1000.0, 3)
        # Persist final timing values added after the first atomic write.
        atomic_write_json(Path(out_dir) / "07_result.json", result)
    else:
        result["runtime"]["total_ms"] = round((perf_counter() - started) * 1000.0, 3)
    return result


def detect_from_path(image_path: str | Path, **kwargs: Any) -> Dict[str, Any]:
    # Load only after validating model configuration so a deployment error is
    # reported consistently even when the image path is also bad.
    model_dir = kwargs.get("model_dir")
    if not model_dir:
        raise VisionModelError(
            "4x model backend requires detect.model_dir containing model_manifest.json and verified weights"
        )
    provider = str(kwargs.get("provider", "cuda"))
    allow_cpu_fallback = bool(kwargs.get("allow_cpu_fallback", False))
    bundle = _cached_bundle(
        str(Path(model_dir).resolve(strict=False)), provider, allow_cpu_fallback
    )
    src = load_image(image_path)
    return detect_from_array(src, src_path=str(image_path), model_bundle=bundle, **kwargs)


def process_image(image_path: str | Path, **kwargs: Any) -> Dict[str, Any]:
    """Workflow entrypoint. ``model_dir`` is mandatory by design."""
    model_dir = kwargs.get("model_dir")
    if not model_dir:
        raise VisionModelError(
            "4x model backend requires detect.model_dir containing model_manifest.json and verified weights"
        )
    return detect_from_path(image_path, **kwargs)


__all__ = ["detect_from_array", "detect_from_path", "process_image"]
