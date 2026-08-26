"""Project per-image clone instances into well coordinates and deduplicate them."""

from __future__ import annotations

from math import hypot
from typing import Any, Dict, Iterable, Sequence

from workflow.plate_geometry import get_axis_pulses_per_mm, get_view_signs


class CloneDedupeError(ValueError):
    """Scan geometry is insufficient or inconsistent for reliable deduplication."""


def _number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except Exception as exc:
        raise CloneDedupeError(f"{name} is required for cross-view deduplication") from exc
    return number


def _stage_base(image: Dict[str, Any]) -> tuple[float, float, Dict[str, str]]:
    actual_x, actual_y = image.get("stage_x_actual"), image.get("stage_y_actual")
    return (
        float(actual_x) if actual_x is not None else _number(image.get("stage_x_target"), "stage_x_target"),
        float(actual_y) if actual_y is not None else _number(image.get("stage_y_target"), "stage_y_target"),
        {"x": "actual" if actual_x is not None else "target", "y": "actual" if actual_y is not None else "target"},
    )


def project_clone_to_stage(
    image: Dict[str, Any], clone: Dict[str, Any], plate_cfg: Dict[str, Any]
) -> Dict[str, Any]:
    """Return the stage pulse target that would center this clone."""
    x_ppm, y_ppm = get_axis_pulses_per_mm(plate_cfg)
    x_sign, y_sign = get_view_signs(plate_cfg)
    base_x, base_y, source = _stage_base(image)
    mm_per_pixel = image.get("mm_per_pixel") or {}
    mm_x = _number(mm_per_pixel.get("x"), "mm_per_pixel.x")
    mm_y = _number(mm_per_pixel.get("y"), "mm_per_pixel.y")
    offset = clone.get("offset_from_image_center_px")
    if not isinstance(offset, Sequence) or isinstance(offset, (str, bytes)) or len(offset) < 2:
        raise CloneDedupeError("offset_from_image_center_px is required")
    dx, dy = float(offset[0]), float(offset[1])
    return {
        "x_pulse": base_x - x_sign * dx * mm_x * x_ppm,
        "y_pulse": base_y - y_sign * dy * mm_y * y_ppm,
        "coordinate_source": source,
    }


def project_bbox_to_well(
    image: Dict[str, Any],
    clone: Dict[str, Any],
    plate_cfg: Dict[str, Any],
    well_start: Dict[str, Any],
) -> Dict[str, Any]:
    stage = project_clone_to_stage(image, clone, plate_cfg)
    x_ppm, y_ppm = get_axis_pulses_per_mm(plate_cfg)
    x_sign, y_sign = get_view_signs(plate_cfg)
    center_right = (stage["x_pulse"] - _number(well_start.get("x"), "well_start.x")) / (x_sign * x_ppm)
    center_down = (stage["y_pulse"] - _number(well_start.get("y"), "well_start.y")) / (y_sign * y_ppm)
    bbox = clone.get("bbox")
    if not isinstance(bbox, Sequence) or isinstance(bbox, (str, bytes)) or len(bbox) < 4:
        raise CloneDedupeError("bbox is required for cross-view deduplication")
    mm_per_pixel = image.get("mm_per_pixel") or {}
    width_mm = max(0.0, float(bbox[2]) * _number(mm_per_pixel.get("x"), "mm_per_pixel.x"))
    height_mm = max(0.0, float(bbox[3]) * _number(mm_per_pixel.get("y"), "mm_per_pixel.y"))
    if width_mm <= 0 or height_mm <= 0:
        raise CloneDedupeError("bbox physical dimensions must be positive")
    image_center = image.get("image_center_px")
    if not isinstance(image_center, Sequence) or isinstance(image_center, (str, bytes)) or len(image_center) < 2:
        raise CloneDedupeError("image_center_px is required for cross-view deduplication")
    base_x, base_y, _ = _stage_base(image)
    bbox_center_dx = float(bbox[0]) + float(bbox[2]) / 2.0 - float(image_center[0])
    bbox_center_dy = float(bbox[1]) + float(bbox[3]) / 2.0 - float(image_center[1])
    bbox_stage_x = base_x - x_sign * bbox_center_dx * _number(mm_per_pixel.get("x"), "mm_per_pixel.x") * x_ppm
    bbox_stage_y = base_y - y_sign * bbox_center_dy * _number(mm_per_pixel.get("y"), "mm_per_pixel.y") * y_ppm
    bbox_center_right = (bbox_stage_x - _number(well_start.get("x"), "well_start.x")) / (x_sign * x_ppm)
    bbox_center_down = (bbox_stage_y - _number(well_start.get("y"), "well_start.y")) / (y_sign * y_ppm)
    return {
        "center_well_mm": [float(center_right), float(center_down)],
        "bbox_center_well_mm": [float(bbox_center_right), float(bbox_center_down)],
        "bbox_well_mm": [
            float(bbox_center_right - width_mm / 2.0),
            float(bbox_center_down - height_mm / 2.0),
            float(width_mm),
            float(height_mm),
        ],
        "center_stage_pulse": [float(stage["x_pulse"]), float(stage["y_pulse"])],
        "coordinate_source": stage["coordinate_source"],
    }


def _intersection_over_min(a: Sequence[float], b: Sequence[float]) -> float:
    ax, ay, aw, ah = [float(v) for v in a[:4]]
    bx, by, bw, bh = [float(v) for v in b[:4]]
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    minimum = min(aw * ah, bw * bh)
    return intersection / minimum if minimum > 0 else 0.0


def _same_clone(
    a: Dict[str, Any],
    b: Dict[str, Any],
    iom_threshold: float,
    registration_tolerance_mm: float,
) -> bool:
    if a["image_index"] == b["image_index"]:
        return False
    if _intersection_over_min(a["bbox_well_mm"], b["bbox_well_mm"]) < iom_threshold:
        return False
    acx, acy = a["bbox_center_well_mm"]
    bcx, bcy = b["bbox_center_well_mm"]
    distance = hypot(acx - bcx, acy - bcy)
    return distance <= registration_tolerance_mm


def _representative_rank(item: Dict[str, Any]) -> tuple[Any, ...]:
    bbox = item["clone"].get("bbox") or [0, 0, 0, 0]
    image = item["image"]
    image_width = float(image.get("image_width_px") or 0)
    image_height = float(image.get("image_height_px") or 0)
    center = item["clone"].get("center_px") or [0, 0]
    edge_distance = min(
        float(center[0]), float(center[1]), image_width - float(center[0]), image_height - float(center[1])
    )
    return (
        bool(item["clone"].get("truncated") or item["clone"].get("touch_image_border")),
        item["clone"].get("segmentation_status") != "success",
        -float(item["clone"].get("detection_confidence") or item["clone"].get("confidence") or 0.0),
        -edge_distance,
        int(item["image_index"]),
        str(item["clone"].get("clone_id") or ""),
        -float(bbox[2]) * float(bbox[3]),
    )


def assign_global_clone_ids(groups: Iterable[list[Dict[str, Any]]]) -> list[Dict[str, Any]]:
    records: list[Dict[str, Any]] = []
    for group in groups:
        representative = min(group, key=_representative_rank)
        weights = [max(1e-6, float(item["clone"].get("detection_confidence") or item["clone"].get("confidence") or 1.0)) for item in group]
        total_weight = sum(weights)
        center = [
            sum(item["center_well_mm"][axis] * weight for item, weight in zip(group, weights)) / total_weight
            for axis in (0, 1)
        ]
        records.append(
            {
                "center_well_mm": [float(center[0]), float(center[1])],
                "bbox_well_mm": list(representative["bbox_well_mm"]),
                "representative": {
                    "image_index": int(representative["image_index"]),
                    "clone_id": str(representative["clone"]["clone_id"]),
                    "source_detection_id": str(representative["source_detection_id"]),
                },
                "source_detections": [
                    str(item["source_detection_id"])
                    for item in sorted(group, key=lambda value: (value["image_index"], value["source_detection_id"]))
                ],
                "observation_count": len(group),
                "confidence": max(float(item["clone"].get("confidence") or 0.0) for item in group),
                "quality_assessment": {
                    "status": "not_assessed",
                    "reason": "4x localization cannot determine iPSC colony quality; inspect at 10x",
                },
            }
        )
    records.sort(key=lambda item: (item["center_well_mm"][1], item["center_well_mm"][0]))
    for index, record in enumerate(records, start=1):
        record["global_clone_id"] = f"G{index:03d}"
    return records


def dedupe_well_clones(
    images: list[Dict[str, Any]],
    *,
    plate_cfg: Dict[str, Any],
    reference: Dict[str, Any],
    scan_config: Dict[str, Any],
    iom_threshold: float = 0.50,
    registration_tolerance_mm: float = 0.10,
) -> Dict[str, Any]:
    """Deduplicate formal clones while retaining every source observation."""
    overlap = float((scan_config or {}).get("overlap") or 0.0)
    well_start = (reference or {}).get("well_start")
    formal_count = sum(len(image.get("clones") or []) for image in images)
    if formal_count == 0:
        return {
            "unique_clones": [],
            "metadata": {
                "method": "physical_bbox_iom_and_center_distance",
                "input_observation_count": 0,
                "merged_observation_count": 0,
                "iom_threshold": float(iom_threshold),
            },
        }
    if not isinstance(well_start, dict):
        if len(images) == 1 or overlap <= 0.0:
            # A single/non-overlapping view cannot contain cross-view duplicates;
            # use a local origin while keeping the result contract deterministic.
            first = images[0]
            base_x, base_y, _ = _stage_base(first)
            well_start = {"x": base_x, "y": base_y}
        else:
            raise CloneDedupeError("reference.well_start is required when overlapping views are present")

    if not 0.0 < float(iom_threshold) <= 1.0:
        raise CloneDedupeError("iom_threshold must be in (0,1]")
    if not 0.0 < float(registration_tolerance_mm):
        raise CloneDedupeError("registration_tolerance_mm must be positive")
    if overlap > 0.0 and not bool((scan_config or {}).get("dedupe_registration_calibrated", False)):
        raise CloneDedupeError(
            "overlapping-view deduplication requires a calibrated registration threshold"
        )
    projected: list[Dict[str, Any]] = []
    source_ids: set[str] = set()
    for image in images:
        for clone in image.get("clones") or []:
            source_id = str(clone.get("source_detection_id") or f"I{image.get('index')}:{clone.get('clone_id')}")
            if source_id in source_ids:
                raise CloneDedupeError(f"duplicate source_detection_id: {source_id}")
            source_ids.add(source_id)
            clone["source_detection_id"] = source_id
            geometry = project_bbox_to_well(image, clone, plate_cfg, well_start)
            projected.append(
                {
                    **geometry,
                    "image_index": int(image["index"]),
                    "source_detection_id": source_id,
                    "image": image,
                    "clone": clone,
                }
            )

    parents = list(range(len(projected)))
    cluster_images: list[set[int]] = [{int(item["image_index"])} for item in projected]

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(a: int, b: int) -> None:
        root_a, root_b = find(a), find(b)
        if root_a == root_b or cluster_images[root_a] & cluster_images[root_b]:
            return
        # Complete-link constraint avoids single-link transitive bridges.  Every
        # cross-image pair in the proposed cluster must independently satisfy
        # the identity gate.
        members_a = [index for index in range(len(projected)) if find(index) == root_a]
        members_b = [index for index in range(len(projected)) if find(index) == root_b]
        if not all(
            _same_clone(
                projected[left],
                projected[right],
                float(iom_threshold),
                float(registration_tolerance_mm),
            )
            for left in members_a
            for right in members_b
        ):
            return
        keep, drop = (root_a, root_b) if root_a < root_b else (root_b, root_a)
        parents[drop] = keep
        cluster_images[keep] |= cluster_images[drop]
        cluster_images[drop] = set()

    for left in range(len(projected)):
        for right in range(left + 1, len(projected)):
            if _same_clone(
                projected[left],
                projected[right],
                float(iom_threshold),
                float(registration_tolerance_mm),
            ):
                union(left, right)

    grouped: Dict[int, list[Dict[str, Any]]] = {}
    for index, item in enumerate(projected):
        grouped.setdefault(find(index), []).append(item)
    unique = assign_global_clone_ids(grouped.values())
    source_to_global = {
        source_id: record["global_clone_id"]
        for record in unique
        for source_id in record["source_detections"]
    }
    for image in images:
        for clone in image.get("clones") or []:
            clone["global_clone_id"] = source_to_global[clone["source_detection_id"]]

    return {
        "unique_clones": unique,
        "metadata": {
            "method": "physical_bbox_iom_and_center_distance",
            "input_observation_count": len(projected),
            "merged_observation_count": len(projected) - len(unique),
            "iom_threshold": float(iom_threshold),
            "registration_tolerance_mm": float(registration_tolerance_mm),
            "center_rule": "absolute bbox-center registration tolerance",
            "coordinate_preference": "actual_then_target_stage",
            "coordinate_frame": {
                "origin": "well_start_left_midline",
                "axes": ["view_right_mm", "view_down_mm"],
            },
            "registration_tolerance_status": "requires calibration with annotated overlapping 4x scans",
        },
    }


__all__ = [
    "CloneDedupeError",
    "assign_global_clone_ids",
    "dedupe_well_clones",
    "project_bbox_to_well",
    "project_clone_to_stage",
]
