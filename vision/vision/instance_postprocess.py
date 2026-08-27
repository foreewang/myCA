"""4x 模型推理使用的切片、框融合和实例 mask 几何工具。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Sequence

import cv2
import numpy as np


@dataclass(frozen=True)
class Tile:
    x: int
    y: int
    width: int
    height: int
    own_x0: float
    own_y0: float
    own_x1: float
    own_y1: float

    def owns_global_center(self, center_x: float, center_y: float) -> bool:
        return (
            self.own_x0 <= center_x < self.own_x1
            and self.own_y0 <= center_y < self.own_y1
        )


def _axis_starts(length: int, tile_size: int, overlap: float) -> list[int]:
    if length <= tile_size:
        return [0]
    stride = max(1, int(round(tile_size * (1.0 - overlap))))
    starts = list(range(0, max(1, length - tile_size + 1), stride))
    last = length - tile_size
    if starts[-1] != last:
        starts.append(last)
    return sorted(set(starts))


def _ownership_edges(starts: Sequence[int], tile_size: int, length: int) -> list[tuple[float, float]]:
    edges: list[tuple[float, float]] = []
    for index, start in enumerate(starts):
        left = 0.0 if index == 0 else (start + starts[index - 1] + tile_size) / 2.0
        right = float(length) if index == len(starts) - 1 else (start + tile_size + starts[index + 1]) / 2.0
        edges.append((left, right))
    return edges


def build_sliding_tiles(
    image_width: int,
    image_height: int,
    *,
    tile_size: int = 1280,
    overlap: float = 0.20,
) -> list[Tile]:
    """生成覆盖整图的确定性切片，并为重叠区域划分唯一中心归属区。"""
    if image_width <= 0 or image_height <= 0:
        raise ValueError("image dimensions must be positive")
    if tile_size <= 0:
        raise ValueError("tile_size must be positive")
    if not 0.0 <= overlap < 1.0:
        raise ValueError("overlap must satisfy 0 <= overlap < 1")

    xs = _axis_starts(image_width, min(tile_size, image_width), overlap)
    ys = _axis_starts(image_height, min(tile_size, image_height), overlap)
    x_edges = _ownership_edges(xs, min(tile_size, image_width), image_width)
    y_edges = _ownership_edges(ys, min(tile_size, image_height), image_height)
    tiles: list[Tile] = []
    for yi, y in enumerate(ys):
        for xi, x in enumerate(xs):
            width = min(tile_size, image_width - x)
            height = min(tile_size, image_height - y)
            tiles.append(
                Tile(
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    own_x0=x_edges[xi][0],
                    own_y0=y_edges[yi][0],
                    own_x1=x_edges[xi][1],
                    own_y1=y_edges[yi][1],
                )
            )
    return tiles


def bbox_iou_xywh(a: Sequence[float], b: Sequence[float]) -> float:
    ax, ay, aw, ah = [float(v) for v in a[:4]]
    bx, by, bw, bh = [float(v) for v in b[:4]]
    ix0, iy0 = max(ax, bx), max(ay, by)
    ix1, iy1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    union = max(0.0, aw) * max(0.0, ah) + max(0.0, bw) * max(0.0, bh) - inter
    return inter / union if union > 0 else 0.0


def bbox_intersection_over_min(a: Sequence[float], b: Sequence[float]) -> float:
    ax, ay, aw, ah = [float(v) for v in a[:4]]
    bx, by, bw, bh = [float(v) for v in b[:4]]
    ix0, iy0 = max(ax, bx), max(ay, by)
    ix1, iy1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    min_area = min(max(0.0, aw) * max(0.0, ah), max(0.0, bw) * max(0.0, bh))
    return inter / min_area if min_area > 0 else 0.0


def _center_and_diag(bbox: Sequence[float]) -> tuple[float, float, float]:
    x, y, w, h = [float(v) for v in bbox[:4]]
    return x + w / 2.0, y + h / 2.0, float(np.hypot(w, h))


def _same_detection(
    a: Dict[str, Any],
    b: Dict[str, Any],
    *,
    iou_threshold: float,
    containment_threshold: float,
    center_ratio: float,
) -> bool:
    bbox_a, bbox_b = a["bbox"], b["bbox"]
    acx, acy, adiag = _center_and_diag(bbox_a)
    bcx, bcy, bdiag = _center_and_diag(bbox_b)
    distance = float(np.hypot(acx - bcx, acy - bcy))
    close_center = distance <= center_ratio * max(1.0, min(adiag, bdiag))
    if bbox_iou_xywh(bbox_a, bbox_b) >= iou_threshold:
        return close_center
    if bbox_intersection_over_min(bbox_a, bbox_b) < containment_threshold:
        return False
    area_a = max(0.0, float(bbox_a[2])) * max(0.0, float(bbox_a[3]))
    area_b = max(0.0, float(bbox_b[2])) * max(0.0, float(bbox_b[3]))
    area_similarity = min(area_a, area_b) / max(area_a, area_b) if max(area_a, area_b) > 0 else 0.0
    return close_center and area_similarity >= 0.75


def merge_detections(
    detections: Iterable[Dict[str, Any]],
    *,
    iou_threshold: float = 0.55,
    containment_threshold: float = 0.70,
    center_ratio: float = 0.25,
) -> list[Dict[str, Any]]:
    """合并全图/切片的重复框，同时保留确定性排序。"""
    ordered = sorted(
        (dict(item) for item in detections),
        key=lambda item: (
            -float(item.get("score", 0.0)),
            int(item["bbox"][1]),
            int(item["bbox"][0]),
            str(item.get("source", "")),
        ),
    )
    kept: list[Dict[str, Any]] = []
    for current in ordered:
        if any(
            str(current.get("source", "")) != str(existing.get("source", ""))
            and
            _same_detection(
                current,
                existing,
                iou_threshold=iou_threshold,
                containment_threshold=containment_threshold,
                center_ratio=center_ratio,
            )
            for existing in kept
        ):
            continue
        kept.append(current)
    return sorted(
        kept,
        key=lambda item: (
            int(round(float(item["bbox"][1]) + float(item["bbox"][3]) / 2.0)),
            int(round(float(item["bbox"][0]) + float(item["bbox"][2]) / 2.0)),
            -float(item.get("score", 0.0)),
        ),
    )


def component_containing_or_nearest_center(
    mask_u8: np.ndarray,
    center_xy: Sequence[float],
    *,
    target_bbox_xywh: Sequence[float] | None = None,
    min_component_area_px: int = 1,
) -> np.ndarray:
    """Select the component best supported by the detector box.

    A tiny noise island under the box centre must not beat the actual colony.
    Components are therefore ranked first by the number of pixels overlapping
    the detector box, then by area and only then by centre distance.
    """
    binary = (mask_u8 > 0).astype(np.uint8)
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if count <= 1:
        return np.zeros_like(binary, dtype=np.uint8)
    cx, cy = float(center_xy[0]), float(center_xy[1])
    candidates = [
        label
        for label in range(1, count)
        if int(stats[label, cv2.CC_STAT_AREA]) >= max(1, int(min_component_area_px))
    ]
    if not candidates:
        return np.zeros_like(binary, dtype=np.uint8)

    target_mask = None
    if target_bbox_xywh is not None:
        x, y, width, height = [float(value) for value in target_bbox_xywh[:4]]
        x0 = max(0, min(binary.shape[1], int(np.floor(x))))
        y0 = max(0, min(binary.shape[0], int(np.floor(y))))
        x1 = max(x0, min(binary.shape[1], int(np.ceil(x + width))))
        y1 = max(y0, min(binary.shape[0], int(np.ceil(y + height))))
        target_mask = np.zeros_like(binary, dtype=bool)
        target_mask[y0:y1, x0:x1] = True

    def rank(label: int) -> tuple[float, float, float]:
        area = float(stats[label, cv2.CC_STAT_AREA])
        overlap = float(np.count_nonzero(np.logical_and(labels == label, target_mask))) if target_mask is not None else 0.0
        distance = float(np.hypot(centroids[label][0] - cx, centroids[label][1] - cy))
        return (-overlap, -area, distance)

    best = min(candidates, key=rank)
    return (labels == best).astype(np.uint8) * 255


def mask_geometry(
    mask_u8: np.ndarray,
    *,
    offset_x: int = 0,
    offset_y: int = 0,
) -> Dict[str, Any] | None:
    """从单实例 mask 计算轮廓、几何质心和最大内接距离点。"""
    binary = (mask_u8 > 0).astype(np.uint8) * 255
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    area = float(cv2.contourArea(contour))
    if area <= 0:
        return None
    epsilon = max(1.0, cv2.arcLength(contour, True) * 0.0015)
    contour = cv2.approxPolyDP(contour, epsilon, True)
    x, y, width, height = cv2.boundingRect(contour)

    moments = cv2.moments(contour)
    if moments["m00"] > 1e-6:
        centroid = [
            int(round(moments["m10"] / moments["m00"])),
            int(round(moments["m01"] / moments["m00"])),
        ]
    else:
        centroid = [int(x + width // 2), int(y + height // 2)]

    distance = cv2.distanceTransform((binary > 0).astype(np.uint8), cv2.DIST_L2, 5)
    _, max_distance, _, max_location = cv2.minMaxLoc(distance)
    safe_point = [int(max_location[0]), int(max_location[1])]
    points = contour[:, 0, :].astype(int)
    points[:, 0] += int(offset_x)
    points[:, 1] += int(offset_y)
    return {
        "bbox": [int(x + offset_x), int(y + offset_y), int(width), int(height)],
        "area_px": int(round(area)),
        "contour_points": points.tolist(),
        "contour_center_pixel": [int(centroid[0] + offset_x), int(centroid[1] + offset_y)],
        "center_pixel": [int(safe_point[0] + offset_x), int(safe_point[1] + offset_y)],
        "safe_point": [int(safe_point[0] + offset_x), int(safe_point[1] + offset_y)],
        "safe_radius_px": float(max_distance),
    }
