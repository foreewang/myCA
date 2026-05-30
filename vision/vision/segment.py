"""克隆目标的粗检测和局部轮廓细化。

本文件是 vision 算法的核心:
- detect_coarse_rois 先在整图上找“可能有克隆”的暗核心区域。
- refine_contour_in_roi 再在每个候选 ROI 内做更精细的轮廓搜索。

代码中的坐标约定:
- bbox 使用 [x, y, w, h]，x/y 是左上角。
- center 使用 [cx, cy]。
- ROI 内坐标需要加上 ROI 左上角偏移量，才能变回原图坐标。
"""

import cv2
import numpy as np

from .center_locator import contour_centroid_from_mask
from .postprocess import circular_smooth, nms_xywh
from .preprocess import auto_seed_threshold, resize_keep_ratio, roi_density_signal


def detect_coarse_rois(
    gray,
    work_max=1024,
    flat_sigma=41,
    seed_thresh=None,
    density_sigma=25,
    close_kernel=35,
    open_kernel=9,
    min_area=10000,
    max_keep=None,
    pad_ratio=0.15,
    nms_iou_thr=0.30,
    border_margin=2,
    border_keep_min_area=50000,
    seed_quantile=0.12,
    seed_hard_floor=35,
    seed_hard_ceil=105,
    core_density_min=80,
    min_foreground_ratio=0.065,
    max_foreground_ratio=0.80,
    min_dark_core_area_ratio=0.00001,
    max_dark_core_area_ratio=0.12,
    max_bbox_area_ratio=0.30,
    min_largest_dark_core_component_ratio=0.12,
    max_dark_core_fragment_count=80,
    min_dark_core_fragment_area=5,
    raw_black_thresh=50,
    min_raw_black_area_small=10000,
    min_raw_black_foreground_ratio=0.010,
    max_raw_black_bbox_aspect=2.80,
    reject_border_touch=False,
):
    """从严格 dark core 中寻找粗候选 ROI。

    这一步故意偏保守: 先找暗核心，再用面积比例、bbox 比例、触边等
    规则过滤异常区域。这样可以减少无克隆背景图或大面积阴影造成的
    误检，为后续补偿流程提供更可靠的候选点。
    """
    small, scale = resize_keep_ratio(gray, work_max=work_max)

    # 背景校正: 用大尺度模糊估计低频背景，再通过除法减弱光照不均。
    bg = cv2.GaussianBlur(small, (0, 0), flat_sigma)
    flat = cv2.normalize(
        cv2.divide(small.astype(np.float32), bg.astype(np.float32) + 1.0, scale=128.0),
        None,
        0,
        255,
        cv2.NORM_MINMAX,
    ).astype(np.uint8)

    if seed_thresh is None:
        seed_thresh = auto_seed_threshold(
            flat,
            q=seed_quantile,
            hard_floor=seed_hard_floor,
            hard_ceil=seed_hard_ceil,
        )

    # dark_seed 是最严格的一层暗区判断，后续所有候选都必须来自它。
    dark_seed = (flat < seed_thresh).astype(np.uint8) * 255
    raw_black = (small <= int(raw_black_thresh)).astype(np.uint8) * 255
    raw_black_clean = cv2.morphologyEx(
        raw_black,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)),
    )
    raw_black_clean = cv2.morphologyEx(
        raw_black_clean,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
    )
    density = cv2.GaussianBlur((dark_seed > 0).astype(np.float32), (0, 0), density_sigma)
    density_u8 = cv2.normalize(density, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

    otsu_thr, _ = cv2.threshold(density_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    density_thr = max(float(otsu_thr), float(core_density_min))
    binary = (density_u8 >= density_thr).astype(np.uint8) * 255
    binary = cv2.morphologyEx(
        binary,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_kernel, close_kernel)),
    )
    binary = cv2.morphologyEx(
        binary,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (open_kernel, open_kernel)),
    )

    # 连通域给出候选块，再逐个做质量过滤。
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)
    Hs, Ws = small.shape
    image_area = float(max(1, Hs * Ws))
    comps = []

    def _border_sides(x, y, w, h):
        sides = []
        if x <= border_margin:
            sides.append("left")
        if y <= border_margin:
            sides.append("top")
        if x + w >= Ws - border_margin:
            sides.append("right")
        if y + h >= Hs - border_margin:
            sides.append("bottom")
        return sides

    def _shape_ok_for_black_marker(x, y, w, h, black_area):
        bbox_area = float(max(1, int(w) * int(h)))
        aspect = float(max(w, h)) / float(max(1, min(w, h)))
        black_ratio = float(black_area) / bbox_area
        return (
            int(black_area) >= int(min_raw_black_area_small)
            and black_ratio >= float(min_raw_black_foreground_ratio)
            and aspect <= float(max_raw_black_bbox_aspect)
        )

    for i in range(1, n):
        x, y, w, h, area = stats[i]
        # 连通域面积不能太小
        if area < min_area:
            continue

        bbox_area = float(max(1, int(w) * int(h)))
        bbox_area_ratio = bbox_area / image_area
        # bbox 太大时通常是背景阴影、整孔边缘或污染，而不是单个克隆。
        if bbox_area_ratio > max_bbox_area_ratio:
            continue

        image_border_sides = _border_sides(x, y, w, h)
        touch_image_border = bool(image_border_sides)
        if len(image_border_sides) >= 2:
            continue
        # 触边候选容易是不完整目标或孔边缘结构，默认拒识。
        comp_mask = labels[y:y + h, x:x + w] == i
        core_roi = dark_seed[y:y + h, x:x + w] > 0
        # 只统计连通域内部真正属于 dark_seed 的像素，避免宽松区域膨胀过头。
        dark_core_mask = comp_mask & core_roi
        dark_core_area = int(np.count_nonzero(dark_core_mask))
        if dark_core_area <= 0:
            continue
        raw_black_area = int(np.count_nonzero(comp_mask & (raw_black[y:y + h, x:x + w] > 0)))
        if not _shape_ok_for_black_marker(x, y, w, h, raw_black_area):
            continue

        core_n, _, core_stats, _ = cv2.connectedComponentsWithStats(
            dark_core_mask.astype(np.uint8),
            connectivity=8,
        )
        if core_n > 1:
            core_areas = core_stats[1:, cv2.CC_STAT_AREA]
            largest_dark_core_component_area = int(core_areas.max())
            dark_core_fragment_count = int(np.count_nonzero(core_areas >= int(min_dark_core_fragment_area)))
        else:
            largest_dark_core_component_area = 0
            dark_core_fragment_count = 0

        largest_dark_core_component_ratio = float(largest_dark_core_component_area) / float(max(1, dark_core_area))
        if largest_dark_core_component_ratio < min_largest_dark_core_component_ratio:
            continue
        if dark_core_fragment_count > max_dark_core_fragment_count and largest_dark_core_component_ratio < 0.30:
            continue

        foreground_ratio = float(dark_core_area) / bbox_area
        dark_core_area_ratio = float(dark_core_area) / image_area
        if foreground_ratio < min_foreground_ratio or foreground_ratio > max_foreground_ratio:
            continue
        if dark_core_area_ratio < min_dark_core_area_ratio:
            continue
        if dark_core_area_ratio > max_dark_core_area_ratio:
            continue

        ys, xs = np.nonzero(dark_core_mask)
        if xs.size:
            core_cx = float(xs.mean() + x)
            core_cy = float(ys.mean() + y)
        else:
            core_cx, core_cy = centroids[i]

        cx, cy = centroids[i]
        # 置信度不是模型概率，只是把当前规则质量压缩到 0~1，供上层排序/拒识。
        confidence = min(1.0, max(0.0, foreground_ratio / 0.18))
        confidence *= min(1.0, max(0.0, 1.0 - bbox_area_ratio / max_bbox_area_ratio))
        if touch_image_border:
            confidence *= 0.5

        comps.append({
            "label": int(i),
            "bbox_small": [int(x), int(y), int(w), int(h)],
            "area_small": int(area),
            "dark_core_area_small": int(dark_core_area),
            "raw_black_area_small": int(raw_black_area),
            "largest_dark_core_component_area_small": int(largest_dark_core_component_area),
            "largest_dark_core_component_ratio": float(largest_dark_core_component_ratio),
            "dark_core_fragment_count": int(dark_core_fragment_count),
            "foreground_ratio": float(foreground_ratio),
            "bbox_area_ratio": float(bbox_area_ratio),
            "dark_core_area_ratio": float(dark_core_area_ratio),
            "center_small": [float(cx), float(cy)],
            "dark_core_center_small": [float(core_cx), float(core_cy)],
            "score": float(dark_core_area) * max(0.05, float(confidence)),
            "confidence": float(confidence),
            "bbox": [int(x), int(y), int(w), int(h)],
            "touch_image_border": bool(touch_image_border),
            "image_border_sides": image_border_sides,
            "image_edge_clipped": bool(touch_image_border),
        })

    # A marker can occupy a large edge-cropped region. In those cases the
    # relative-dark density pass may reject it as too broad, so add compact
    # absolute-black connected components as first-class coarse candidates.
    black_n, black_labels, black_stats, black_centroids = cv2.connectedComponentsWithStats(
        raw_black_clean,
        connectivity=8,
    )
    for i in range(1, black_n):
        x, y, w, h, area = black_stats[i]
        black_area = int(area)
        if not _shape_ok_for_black_marker(x, y, w, h, black_area):
            continue

        bbox_area = float(max(1, int(w) * int(h)))
        bbox_area_ratio = bbox_area / image_area
        if bbox_area_ratio > max_bbox_area_ratio:
            continue

        image_border_sides = _border_sides(x, y, w, h)
        touch_image_border = bool(image_border_sides)
        if len(image_border_sides) >= 2:
            continue
        if reject_border_touch and touch_image_border and black_area < border_keep_min_area:
            continue

        core_cx, core_cy = black_centroids[i]
        foreground_ratio = float(black_area) / bbox_area
        dark_core_area_ratio = float(black_area) / image_area
        if dark_core_area_ratio < min_dark_core_area_ratio:
            continue

        aspect = float(max(w, h)) / float(max(1, min(w, h)))
        confidence = min(1.0, foreground_ratio / 0.18)
        confidence *= min(1.0, max(0.0, 1.0 - (aspect - 1.0) / max(1e-6, max_raw_black_bbox_aspect - 1.0)))

        comps.append({
            "label": int(i),
            "bbox_small": [int(x), int(y), int(w), int(h)],
            "area_small": int(black_area),
            "dark_core_area_small": int(black_area),
            "raw_black_area_small": int(black_area),
            "largest_dark_core_component_area_small": int(black_area),
            "largest_dark_core_component_ratio": 1.0,
            "dark_core_fragment_count": 1,
            "foreground_ratio": float(foreground_ratio),
            "bbox_area_ratio": float(bbox_area_ratio),
            "dark_core_area_ratio": float(dark_core_area_ratio),
            "center_small": [float(core_cx), float(core_cy)],
            "dark_core_center_small": [float(core_cx), float(core_cy)],
            "score": float(black_area) * max(0.05, float(confidence)),
            "confidence": float(confidence),
            "bbox": [int(x), int(y), int(w), int(h)],
            "touch_image_border": bool(touch_image_border),
            "image_border_sides": image_border_sides,
            "image_edge_clipped": bool(touch_image_border),
        })

    # NMS 去掉高度重叠的候选，保留分数更高的那个。
    comps = nms_xywh(comps, key_score="score", key_bbox="bbox", iou_thr=nms_iou_thr)
    comps = [comp for comp in comps if float(comp.get("confidence", 0.0) or 0.0) >= 0.25]
    comps = nms_xywh(comps, key_score="score", key_bbox="bbox", iou_thr=nms_iou_thr)
    comps.sort(key=lambda d: d["score"], reverse=True)
    if max_keep is not None and max_keep > 0:
        comps = comps[:max_keep]

    H, W = gray.shape
    results = []
    for comp in comps:
        x, y, w, h = comp["bbox_small"]
        cx, cy = comp["center_small"]
        core_cx, core_cy = comp["dark_core_center_small"]

        # 前面的检测在 small 图上完成，这里映射回原图坐标。
        X = int(round(x * scale))
        Y = int(round(y * scale))
        Bw = int(round(w * scale))
        Bh = int(round(h * scale))
        Cx = int(round(cx * scale))
        Cy = int(round(cy * scale))
        CoreCx = int(round(core_cx * scale))
        CoreCy = int(round(core_cy * scale))

        pad_x = int(round(Bw * pad_ratio))
        pad_y = int(round(Bh * pad_ratio))

        x0 = max(0, X - pad_x)
        y0 = max(0, Y - pad_y)
        x1 = min(W, X + Bw + pad_x)
        y1 = min(H, Y + Bh + pad_y)

        results.append({
            "coarse_bbox": [int(x0), int(y0), int(x1 - x0), int(y1 - y0)],
            "coarse_center_pixel": [int(Cx), int(Cy)],
            "dark_core_center_pixel": [int(CoreCx), int(CoreCy)],
            "safe_point": [int(CoreCx), int(CoreCy)],
            "area_small": int(comp["area_small"]),
            "dark_core_area_small": int(comp["dark_core_area_small"]),
            "raw_black_area_small": int(comp.get("raw_black_area_small", 0)),
            "largest_dark_core_component_area_small": int(comp.get("largest_dark_core_component_area_small", 0)),
            "largest_dark_core_component_ratio": float(comp.get("largest_dark_core_component_ratio", 0.0)),
            "dark_core_fragment_count": int(comp.get("dark_core_fragment_count", 0)),
            "foreground_ratio": float(comp["foreground_ratio"]),
            "bbox_area_ratio": float(comp["bbox_area_ratio"]),
            "dark_core_area_ratio": float(comp["dark_core_area_ratio"]),
            "touch_image_border": bool(comp["touch_image_border"]),
            "image_border_sides": list(comp.get("image_border_sides") or []),
            "image_edge_clipped": bool(comp.get("image_edge_clipped", False)),
            "confidence": float(comp["confidence"]),
            "is_valid_for_compensation": bool(comp["confidence"] >= 0.25),
        })

    debug = {
        "small_gray": small,
        "flat": flat,
        "dark_seed": dark_seed,
        "density_u8": density_u8,
        "binary_small": binary,
        "scale": scale,
        "seed_thresh": int(seed_thresh),
        "density_thresh": float(density_thr),
        "coarse_candidate_count": len(results),
    }
    return results, debug


def radial_contour_from_signal_vectorized(
    signal_u8,
    center_xy,
    n_angles=180,
    inner_ratio=0.12,
    border_strip=20,
    target_alpha=0.52,
    min_radius=10,
    mode="hybrid",
    grad_refine_window=18,
    fallback_radius_ratio=0.18,
):
    """在密度图上沿多个角度做径向采样，估计目标轮廓。

    思路类似“从中心向外发射射线”: 每个角度找到一个边界半径，
    再把所有角度的点连起来形成闭合轮廓。mode 控制边界选择策略:
    - threshold: 使用密度阈值穿越位置。
    - gradient: 使用梯度变化最大的位置。
    - hybrid: 先用阈值定位，再在附近用梯度微调。
    """
    H, W = signal_u8.shape
    cx, cy = float(center_xy[0]), float(center_xy[1])

    yy, xx = np.indices(signal_u8.shape)
    rr = np.hypot(xx - cx, yy - cy)

    inner_r = max(8, int(min(H, W) * inner_ratio))
    inside_mask = rr <= inner_r
    inside_level = float(signal_u8[inside_mask].mean()) if np.any(inside_mask) else float(signal_u8.mean())

    strip = min(border_strip, max(2, min(H, W) // 10))
    border_vals = np.concatenate([
        signal_u8[:strip, :].ravel(),
        signal_u8[-strip:, :].ravel(),
        signal_u8[:, :strip].ravel(),
        signal_u8[:, -strip:].ravel(),
    ])
    outside_level = float(border_vals.mean())
    target = outside_level + target_alpha * (inside_level - outside_level)

    max_r = int(min(cx - 2, cy - 2, W - cx - 3, H - cy - 3))
    max_r = max(max_r, min_radius + 5)

    angles = np.linspace(0.0, 2.0 * np.pi, n_angles, endpoint=False, dtype=np.float32)
    rs = np.arange(min_radius, max_r, dtype=np.float32)
    if rs.size == 0:
        rs = np.arange(min_radius, min_radius + 6, dtype=np.float32)

    cos_t = np.cos(angles)[:, None]
    sin_t = np.sin(angles)[:, None]

    xs = np.clip(np.round(cx + cos_t * rs[None, :]).astype(np.int32), 0, W - 1)
    ys = np.clip(np.round(cy + sin_t * rs[None, :]).astype(np.int32), 0, H - 1)

    # profiles 的形状为 [角度数, 半径采样数]。
    profiles = signal_u8[ys, xs].astype(np.float32)
    kx = cv2.getGaussianKernel(ksize=9, sigma=2.0).astype(np.float32).reshape(-1)
    profiles = cv2.sepFilter2D(
        profiles,
        ddepth=-1,
        kernelX=kx,
        kernelY=np.array([1.0], dtype=np.float32),
        borderType=cv2.BORDER_REPLICATE,
    )

    cond = profiles > target
    any_hit = cond.any(axis=1)

    last_idx = np.zeros((n_angles,), dtype=np.int32)
    if profiles.shape[1] > 0:
        rev_argmax = np.argmax(cond[:, ::-1], axis=1)
        last_idx = cond.shape[1] - 1 - rev_argmax

    fallback_r = max(min_radius, int(min(H, W) * fallback_radius_ratio))
    radii_thr = np.full((n_angles,), fallback_r, dtype=np.float32)
    radii_thr[any_hit] = rs[last_idx[any_hit]]

    if mode == "threshold":
        radii = radii_thr
    else:
        grad = np.diff(profiles, axis=1)
        radii = radii_thr.copy()

        for i in range(n_angles):
            if not any_hit[i]:
                if mode == "gradient":
                    j = int(np.argmin(grad[i]))
                    radii[i] = rs[min(j, rs.size - 1)]
                continue

            base_j = int(last_idx[i])
            if mode == "gradient":
                j0 = max(0, int(rs.size * 0.15))
                j1 = grad.shape[1]
            else:
                j0 = max(0, base_j - grad_refine_window)
                j1 = min(grad.shape[1], base_j + grad_refine_window)

            if j1 <= j0:
                continue
            local = grad[i, j0:j1]
            j = j0 + int(np.argmin(local))
            radii[i] = rs[min(j, rs.size - 1)]

    radii = circular_smooth(radii, window=11)
    pts = np.stack([
        cx + radii * np.cos(angles),
        cy + radii * np.sin(angles),
    ], axis=1).astype(np.int32)
    return pts, target, radii


def _dark_core_center_in_mask(dark_seed_u8, mask_u8, fallback_center, scale):
    """优先用轮廓内部 dark core 的平均位置作为安全中心点。"""
    core = (dark_seed_u8 > 0) & (mask_u8 > 0)
    ys, xs = np.nonzero(core)
    if xs.size == 0:
        return [int(round(fallback_center[0] * scale)), int(round(fallback_center[1] * scale))]
    return [int(round(float(xs.mean()) * scale)), int(round(float(ys.mean()) * scale))]


def refine_contour_in_roi(
    roi_gray,
    center_hint_local,
    max_work=1200,
    dark_percentile=42,
    density_sigma=12,
    radial_target_alpha=0.52,
    n_angles=180,
    radial_mode="hybrid",
    recenter_iterations=1,
    recenter_min_shift_px=6.0,
):
    """在单个 ROI 内细化轮廓，并返回 ROI 局部坐标结果。

    center_hint_local 通常来自粗检测的 dark core center / safe point。
    细化过程会先构建暗区密度图，再进行径向轮廓搜索；如果开启
    recenter_iterations，会用当前 mask 质心更新中心并重复搜索。
    """
    small, flat, dark_seed_u8, density_u8, scale = roi_density_signal(
        roi_gray,
        max_work=max_work,
        dark_percentile=dark_percentile,
        density_sigma=density_sigma,
    )

    center_history = []
    cx = float(center_hint_local[0]) / scale
    cy = float(center_hint_local[1]) / scale
    cx = float(np.clip(cx, 5, small.shape[1] - 6))
    cy = float(np.clip(cy, 5, small.shape[0] - 6))
    center_history.append([float(cx), float(cy)])

    pts = None
    target = None
    mask_small = None

    for _ in range(max(1, recenter_iterations + 1)):
        # 从当前中心出发，沿一圈角度估计轮廓点。
        pts, target, _ = radial_contour_from_signal_vectorized(
            density_u8,
            (cx, cy),
            n_angles=n_angles,
            target_alpha=radial_target_alpha,
            mode=radial_mode,
        )

        mask_small = np.zeros_like(density_u8, dtype=np.uint8)
        cv2.fillPoly(mask_small, [pts.reshape(-1, 1, 2)], 255)
        mask_small = cv2.morphologyEx(
            mask_small,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)),
        )
        mask_small = cv2.morphologyEx(
            mask_small,
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
        )

        # 用当前 mask 的质心修正下一轮中心；位移很小时提前停止。
        new_center = contour_centroid_from_mask(mask_small, [cx, cy])
        shift = float(np.hypot(new_center[0] - cx, new_center[1] - cy))
        cx, cy = float(new_center[0]), float(new_center[1])
        cx = float(np.clip(cx, 5, small.shape[1] - 6))
        cy = float(np.clip(cy, 5, small.shape[0] - 6))
        center_history.append([float(cx), float(cy)])

        if shift < recenter_min_shift_px:
            break

    if mask_small is None:
        mask_small = np.zeros_like(density_u8, dtype=np.uint8)

    if scale > 1.0:
        mask_full = cv2.resize(mask_small, (roi_gray.shape[1], roi_gray.shape[0]), interpolation=cv2.INTER_NEAREST)
    else:
        mask_full = mask_small.copy()

    cnts, _ = cv2.findContours(mask_full, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None, {
            "flat": flat,
            "dark_seed": dark_seed_u8,
            "density": density_u8,
            "mask_small": mask_small,
            "scale": scale,
            "target": float(target) if target is not None else None,
            "center_history_small": center_history,
        }

    cnt = max(cnts, key=cv2.contourArea)
    eps = max(2.0, 0.0035 * cv2.arcLength(cnt, True))
    cnt = cv2.approxPolyDP(cnt, eps, True)

    M = cv2.moments(cnt)
    if M["m00"] > 1e-6:
        contour_center = [int(M["m10"] / M["m00"]), int(M["m01"] / M["m00"])]
    else:
        contour_center = [int(center_hint_local[0]), int(center_hint_local[1])]

    # 补偿中心优先使用 dark core 内部点，比单纯轮廓质心更不容易受边缘毛刺影响。
    safe_point = _dark_core_center_in_mask(dark_seed_u8, mask_small, [cx, cy], scale)
    x, y, w, h = cv2.boundingRect(cnt)

    return {
        "contour_local": cnt[:, 0, :].astype(int).tolist(),
        "center_local": safe_point,
        "contour_center_local": contour_center,
        "safe_point_local": safe_point,
        "bbox_local": [int(x), int(y), int(w), int(h)],
        "area_px": int(cv2.contourArea(cnt)),
        "mask_full": mask_full,
        "center_history_small": center_history,
    }, {
        "flat": flat,
        "dark_seed": dark_seed_u8,
        "density": density_u8,
        "mask_small": mask_small,
        "scale": scale,
        "target": float(target),
        "center_history_small": center_history,
    }
