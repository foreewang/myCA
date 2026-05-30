"""Visual well-border detection and pickability annotation.

The well edge is a visual concept here: if the culture-well rim is visible in
the current image, this module estimates its circular boundary from image
edges and uses the clone center's distance to that visual boundary to decide
whether the clone is pickable.
"""

from __future__ import annotations

from typing import Any, Dict, List

import cv2
import numpy as np


def _coerce_mm_per_pixel(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("mm_per_pixel") or value.get("x") or value.get("mm_per_pixel_x")
    try:
        out = float(value)
    except Exception:
        return None
    return out if out > 0 else None


def _edge_support(edges: np.ndarray, cx: float, cy: float, r: float) -> float:
    h, w = edges.shape
    angles = np.linspace(0.0, 2.0 * np.pi, 720, endpoint=False)
    xs = np.round(cx + r * np.cos(angles)).astype(np.int32)
    ys = np.round(cy + r * np.sin(angles)).astype(np.int32)
    inside = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
    if int(np.count_nonzero(inside)) < 80:
        return 0.0
    return float(np.count_nonzero(edges[ys[inside], xs[inside]] > 0)) / float(np.count_nonzero(inside))


def estimate_well_circle(gray: np.ndarray, work_max: int = 1200) -> Dict[str, Any]:
    """Estimate the visible well rim as a circle from image edges.

    Returns a dict with ``detected=false`` when no reliable rim is visible.
    This keeps center-field images from being rejected just because the well
    border is outside the current field of view.
    """
    h, w = gray.shape[:2]
    scale = max(1.0, max(h, w) / float(work_max))
    if scale > 1.0:
        small = cv2.resize(
            gray,
            (max(1, int(round(w / scale))), max(1, int(round(h / scale)))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        small = gray.copy()

    blur = cv2.GaussianBlur(small, (0, 0), 2.0)
    edges = cv2.Canny(blur, 40, 120)
    edges = cv2.morphologyEx(
        edges,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
    )

    sh, sw = small.shape[:2]
    min_dim = min(sh, sw)
    circles = cv2.HoughCircles(
        blur,
        cv2.HOUGH_GRADIENT,
        dp=1.4,
        minDist=max(40, min_dim // 3),
        param1=120,
        param2=24,
        minRadius=max(20, int(min_dim * 0.18)),
        maxRadius=max(30, int(min_dim * 1.2)),
    )

    best = None
    if circles is not None:
        for cx, cy, r in np.round(circles[0]).astype(np.float32):
            support = _edge_support(edges, float(cx), float(cy), float(r))
            if support < 0.035:
                continue
            score = support * min(1.0, float(r) / max(1.0, min_dim * 0.35))
            if best is None or score > best["score"]:
                best = {
                    "cx": float(cx) * scale,
                    "cy": float(cy) * scale,
                    "radius": float(r) * scale,
                    "score": float(score),
                    "edge_support": float(support),
                }

    if best is None:
        return {
            "detected": False,
            "method": "hough_circle",
            "confidence": 0.0,
        }

    return {
        "detected": True,
        "method": "hough_circle",
        "confidence": float(min(1.0, best["score"] * 12.0)),
        "circle": {
            "center_px": [int(round(best["cx"])), int(round(best["cy"]))],
            "radius_px": int(round(best["radius"])),
        },
        "edge_support": float(best["edge_support"]),
    }


def annotate_pickability_from_visual_well_border(
    gray: np.ndarray,
    components: List[Dict[str, Any]],
    *,
    mm_per_pixel: Any = None,
    well_border_margin_mm: float = 0.0,
    well_border_margin_px: float = 30.0,
    enabled: bool = True,
) -> Dict[str, Any]:
    """Add visual well-border distance and is_pickable to each component."""
    mm_px = _coerce_mm_per_pixel(mm_per_pixel)
    if mm_px is not None and well_border_margin_mm > 0:
        margin_px = float(well_border_margin_mm) / mm_px
    else:
        margin_px = float(well_border_margin_px)

    detection = estimate_well_circle(gray) if enabled else {"detected": False, "method": "disabled", "confidence": 0.0}
    circle = detection.get("circle") if detection.get("detected") else None

    for item in components:
        is_valid = item.get("is_valid_for_compensation") is not False
        distance_px = None
        distance_mm = None
        near = False

        if circle:
            cx, cy = item.get("center_pixel") or item.get("safe_point") or [None, None]
            if cx is not None and cy is not None:
                wx, wy = circle["center_px"]
                r = float(circle["radius_px"])
                radial = float(np.hypot(float(cx) - float(wx), float(cy) - float(wy)))
                distance_px = r - radial
                distance_mm = distance_px * mm_px if mm_px is not None else None
                near = distance_px <= margin_px

        item["well_border_detected"] = bool(circle)
        item["well_border_detection"] = detection
        item["near_well_border"] = bool(near)
        item["distance_to_well_edge_px"] = None if distance_px is None else float(distance_px)
        item["distance_to_well_edge_mm"] = None if distance_mm is None else float(distance_mm)
        item["is_pickable"] = bool(is_valid and not near)

    return detection
