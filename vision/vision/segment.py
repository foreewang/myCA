"""Rule detection and refinement backed by connected texture instances."""
import cv2
import numpy as np

from .gpu_ops import DEFAULT_TEXTURE_BACKEND
from .texture_segment import coarse_texture_rois, refine_texture_roi


def detect_coarse_rois(
    gray,
    work_max=1024,
    min_area=10000,
    max_keep=None,
    border_margin=2,
    max_bbox_area_ratio=0.30,
    reject_border_touch=False,
    *, texture_backend=DEFAULT_TEXTURE_BACKEND, texture_noise_floor=1.5, texture_window=7,
):
    """Detect multiple texture-supported instances."""
    return coarse_texture_rois(gray, work_max=work_max, min_area=min_area,
        max_keep=max_keep, max_bbox_area_ratio=max_bbox_area_ratio,
        reject_border_touch=reject_border_touch, border_margin=border_margin,
        texture_backend=texture_backend, texture_noise_floor=texture_noise_floor,
        texture_window=texture_window)


def _largest_centered_component(mask_u8, center_xy):
    """保留包含中心点的连通域；中心点不在前景内时退回最大连通域。"""
    n, labels, stats, _ = cv2.connectedComponentsWithStats((mask_u8 > 0).astype(np.uint8), connectivity=8)
    if n <= 1:
        return np.zeros_like(mask_u8, dtype=np.uint8)

    cx = int(round(center_xy[0]))
    cy = int(round(center_xy[1]))
    if 0 <= cx < mask_u8.shape[1] and 0 <= cy < mask_u8.shape[0]:
        center_label = int(labels[cy, cx])
        if center_label > 0:
            return (labels == center_label).astype(np.uint8) * 255

    best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return (labels == best).astype(np.uint8) * 255


def _edge_refine_mask_grabcut(
    roi_gray,
    base_mask_u8,
    center_xy,
    allowed_mask_u8=None,
    iterations=2,
):
    """用 GrabCut 细化纹理 mask，同时限制结果不能跑出 allowed ROI。"""
    base_mask_u8 = (base_mask_u8 > 0).astype(np.uint8) * 255
    base_area = int(np.count_nonzero(base_mask_u8))
    if base_area <= 0:
        return base_mask_u8, False, {"edge_refine_reason": "empty_base_mask"}

    if allowed_mask_u8 is None:
        allowed_mask_u8 = np.ones_like(base_mask_u8, dtype=np.uint8) * 255
    else:
        allowed_mask_u8 = (allowed_mask_u8 > 0).astype(np.uint8) * 255

    if int(np.count_nonzero(allowed_mask_u8)) <= 0:
        return base_mask_u8, False, {"edge_refine_reason": "empty_allowed_mask"}

    h, w = roi_gray.shape[:2]
    image_bgr = cv2.cvtColor(roi_gray, cv2.COLOR_GRAY2BGR)
    gc_mask = np.full((h, w), cv2.GC_BGD, dtype=np.uint8)
    gc_mask[allowed_mask_u8 > 0] = cv2.GC_PR_BGD

    k = max(3, int(round(min(h, w) * 0.012)))
    if k % 2 == 0:
        k += 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    probable_fg = cv2.dilate(base_mask_u8, kernel, iterations=1)
    sure_fg = cv2.erode(base_mask_u8, kernel, iterations=1)

    gc_mask[(probable_fg > 0) & (allowed_mask_u8 > 0)] = cv2.GC_PR_FGD
    gc_mask[(sure_fg > 0) & (allowed_mask_u8 > 0)] = cv2.GC_FGD
    if not np.any(gc_mask == cv2.GC_FGD):
        gc_mask[(base_mask_u8 > 0) & (allowed_mask_u8 > 0)] = cv2.GC_FGD

    try:
        bgd_model = np.zeros((1, 65), np.float64)
        fgd_model = np.zeros((1, 65), np.float64)
        cv2.grabCut(
            image_bgr,
            gc_mask,
            None,
            bgd_model,
            fgd_model,
            min(2, max(1, int(iterations))),
            cv2.GC_INIT_WITH_MASK,
        )
    except cv2.error as exc:
        return base_mask_u8, False, {"edge_refine_reason": f"grabcut_error:{exc.code}"}

    refined = np.where(
        (gc_mask == cv2.GC_FGD) | (gc_mask == cv2.GC_PR_FGD),
        255,
        0,
    ).astype(np.uint8)
    refined = cv2.bitwise_and(refined, allowed_mask_u8)
    refined = cv2.morphologyEx(
        refined,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
    )
    refined = cv2.morphologyEx(
        refined,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
    )
    refined = _largest_centered_component(refined, center_xy)

    refined_area = int(np.count_nonzero(refined))
    area_ratio = float(refined_area) / float(max(1, base_area))
    cx = int(round(center_xy[0]))
    cy = int(round(center_xy[1]))
    center_inside = 0 <= cx < w and 0 <= cy < h and refined[cy, cx] > 0
    if not center_inside:
        return base_mask_u8, False, {
            "edge_refine_reason": "center_outside",
            "edge_refine_area_ratio": area_ratio,
        }
    if area_ratio < 0.18 or area_ratio > 1.25:
        return base_mask_u8, False, {
            "edge_refine_reason": "area_ratio_rejected",
            "edge_refine_area_ratio": area_ratio,
        }

    return refined, True, {
        "edge_refine_reason": "accepted",
        "edge_refine_area_ratio": area_ratio,
    }


def refine_contour_in_roi(
    roi_gray,
    center_hint_local,
    max_work=1200,
    clip_bbox_local=None,
    clip_pad_ratio=0.05,
    edge_refine_method="none",
    edge_refine_iterations=2,
    *, instance_support=None, texture_backend=DEFAULT_TEXTURE_BACKEND, texture_noise_floor=1.5,
    texture_window=7, safe_margin_px=1.0,
):
    """Refine actual connected support, then choose a point inside final geometry."""
    method = str(edge_refine_method or "none").lower()
    if method not in {"none", "grabcut", "hybrid"}:
        raise ValueError("edge_refine_method must be none, grabcut or hybrid")
    if not np.isfinite(safe_margin_px) or safe_margin_px < 0:
        raise ValueError("safe_margin_px must be finite and nonnegative")
    return refine_texture_roi(roi_gray, center_hint_local, max_work=max_work,
        clip_bbox_local=clip_bbox_local, clip_pad_ratio=clip_pad_ratio,
        instance_support=instance_support, texture_backend=texture_backend,
        texture_noise_floor=texture_noise_floor, texture_window=texture_window,
        safe_margin_px=safe_margin_px, edge_refine_method=method,
        edge_refine_iterations=edge_refine_iterations, grabcut=_edge_refine_mask_grabcut)
