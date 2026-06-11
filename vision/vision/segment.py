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
from .postprocess import bbox_iou_xywh, circular_smooth
from .preprocess import auto_seed_threshold, resize_keep_ratio, roi_density_signal


def _bbox_intersection_over_min(a, b):
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ax1, ay1 = ax0 + aw, ay0 + ah
    bx1, by1 = bx0 + bw, by0 + bh

    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    iw = max(0, ix1 - ix0)
    ih = max(0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0

    area_a = max(0, aw) * max(0, ah)
    area_b = max(0, bw) * max(0, bh)
    min_area = min(area_a, area_b)
    if min_area <= 0:
        return 0.0
    return float(inter) / float(min_area)


def _nms_coarse_candidates(items, iou_thr=0.30, containment_thr=0.60):
    if not items:
        return []

    order = sorted(items, key=lambda d: float(d.get("score", 0.0)), reverse=True)
    keep = []
    for cur in order:
        cur_bbox = cur["bbox"]
        cur_source = cur.get("source", "dark")
        ok = True
        for kept in keep:
            kept_bbox = kept["bbox"]
            if bbox_iou_xywh(cur_bbox, kept_bbox) > iou_thr:
                ok = False
                break

            overlap_min = _bbox_intersection_over_min(cur_bbox, kept_bbox)
            if overlap_min >= containment_thr and (
                cur_source == "texture" or kept.get("source", "dark") == "texture"
            ):
                ok = False
                break
        if ok:
            keep.append(cur)
    return keep


def _dedupe_mapped_coarse_results(results, containment_thr=0.60):
    keep = []
    for cur in results:
        cur_source = cur.get("detection_source", "dark")
        duplicate = False
        for kept in keep:
            overlap_min = _bbox_intersection_over_min(cur["coarse_bbox"], kept["coarse_bbox"])
            if overlap_min < containment_thr:
                continue

            kept_source = kept.get("detection_source", "dark")
            if cur_source == "texture" and kept_source != "texture":
                duplicate = True
                break
            if kept_source == "texture" and cur_source != "texture":
                kept.update(cur)
                duplicate = True
                break
        if not duplicate:
            keep.append(cur)
    return keep


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
    seed_quantile=0.12,
    seed_hard_floor=35,
    seed_hard_ceil=105,
    core_density_min=80,
    min_foreground_ratio=0.025,
    max_foreground_ratio=0.80,
    min_dark_core_area_ratio=0.00001,
    max_dark_core_area_ratio=0.12,
    max_bbox_area_ratio=0.30,
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
        if reject_border_touch and touch_image_border:
            continue
        # 触边候选容易是不完整目标或孔边缘结构，默认拒识。
        comp_mask = labels[y:y + h, x:x + w] == i
        core_roi = dark_seed[y:y + h, x:x + w] > 0
        # 只统计连通域内部真正属于 dark_seed 的像素，避免宽松区域膨胀过头。
        dark_core_mask = comp_mask & core_roi
        dark_core_area = int(np.count_nonzero(dark_core_mask))
        if dark_core_area <= 0:
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

    # NMS 去掉高度重叠的候选，保留分数更高的那个。
    # Texture-density fallback for colonies that are textured but not dark
    # enough to survive the strict dark-core seed path.
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(small)
    texture_bg = cv2.GaussianBlur(enhanced, (0, 0), 45)
    texture_flat = cv2.normalize(
        cv2.divide(enhanced.astype(np.float32), texture_bg.astype(np.float32) + 1.0, scale=128.0),
        None,
        0,
        255,
        cv2.NORM_MINMAX,
    ).astype(np.uint8)
    texture_blur = cv2.GaussianBlur(texture_flat, (0, 0), 1.2)
    gx = cv2.Sobel(texture_blur, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(texture_blur, cv2.CV_32F, 0, 1, ksize=3)
    grad = cv2.magnitude(gx, gy)
    valid_intensity = (small > 20) & (small < 245)
    valid_grad = grad[valid_intensity]
    if valid_grad.size:
        grad_thr = float(np.percentile(valid_grad, 92.0))
        texture_seed = ((grad > grad_thr) & valid_intensity).astype(np.float32)
        texture_density = cv2.GaussianBlur(texture_seed, (0, 0), 18)
        texture_density_u8 = cv2.normalize(texture_density, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        texture_otsu, _ = cv2.threshold(texture_density_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        texture_binary = (texture_density_u8 >= max(30.0, float(texture_otsu) * 1.2)).astype(np.uint8) * 255
        texture_binary = cv2.morphologyEx(
            texture_binary,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31)),
        )
        texture_binary = cv2.morphologyEx(
            texture_binary,
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 17)),
        )

        tn, tlabels, tstats, tcentroids = cv2.connectedComponentsWithStats(texture_binary, connectivity=8)
        texture_min_area = max(int(min_area), int(round(image_area * 0.008)))
        for ti in range(1, tn):
            x, y, w, h, area = tstats[ti]
            if area < texture_min_area:
                continue
            bbox_area = float(max(1, int(w) * int(h)))
            bbox_area_ratio = bbox_area / image_area
            if bbox_area_ratio > max_bbox_area_ratio:
                continue
            aspect = float(w) / float(max(1, h))
            if aspect < 0.55 or aspect > 1.80:
                continue
            image_border_sides = _border_sides(x, y, w, h)
            if image_border_sides:
                continue

            comp_u8 = (tlabels[y:y + h, x:x + w] == ti).astype(np.uint8) * 255
            cnts, _ = cv2.findContours(comp_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not cnts:
                continue
            cnt = max(cnts, key=cv2.contourArea)
            perimeter = cv2.arcLength(cnt, True)
            contour_area = cv2.contourArea(cnt)
            circularity = float(4.0 * np.pi * contour_area / (perimeter * perimeter + 1e-6))
            if circularity < 0.50:
                continue

            comp_mask_full = tlabels == ti
            mean_density = float(texture_density_u8[comp_mask_full].mean()) if np.any(comp_mask_full) else 0.0
            mean_intensity = float(small[comp_mask_full].mean()) if np.any(comp_mask_full) else 0.0
            if mean_intensity < 35.0 or mean_intensity > 230.0:
                continue

            comp_mask = tlabels[y:y + h, x:x + w] == ti
            core_roi = dark_seed[y:y + h, x:x + w] > 0
            dark_core_mask = comp_mask & core_roi
            dark_core_area = int(np.count_nonzero(dark_core_mask))
            foreground_ratio = float(dark_core_area) / bbox_area
            dark_core_area_ratio = float(dark_core_area) / image_area
            if foreground_ratio > max_foreground_ratio or dark_core_area_ratio > max_dark_core_area_ratio:
                continue

            ys, xs = np.nonzero(dark_core_mask)
            if xs.size:
                core_cx = float(xs.mean() + x)
                core_cy = float(ys.mean() + y)
            else:
                core_cx, core_cy = tcentroids[ti]
            cx, cy = tcentroids[ti]

            shape_score = min(1.0, circularity / 0.80)
            texture_score = min(1.0, mean_density / 135.0)
            confidence = 0.55 * shape_score * texture_score
            confidence *= min(1.0, max(0.0, 1.0 - bbox_area_ratio / max_bbox_area_ratio))
            score = float(area) * max(0.05, confidence) * 1.35
            comps.append({
                "label": int(ti),
                "bbox_small": [int(x), int(y), int(w), int(h)],
                "area_small": int(area),
                "dark_core_area_small": int(dark_core_area),
                "foreground_ratio": float(foreground_ratio),
                "bbox_area_ratio": float(bbox_area_ratio),
                "dark_core_area_ratio": float(dark_core_area_ratio),
                "center_small": [float(cx), float(cy)],
                "dark_core_center_small": [float(core_cx), float(core_cy)],
                "score": float(score),
                "confidence": float(confidence),
                "bbox": [int(x), int(y), int(w), int(h)],
                "touch_image_border": False,
                "image_border_sides": [],
                "image_edge_clipped": False,
                "source": "texture",
            })

    comps = _nms_coarse_candidates(comps, iou_thr=nms_iou_thr)
    strong_texture = [
        c for c in comps
        if c.get("source") == "texture"
        and not c.get("touch_image_border")
        and float(c.get("confidence", 0.0)) >= 0.25
    ]
    if strong_texture:
        best_texture_score = max(float(c.get("score", 0.0)) for c in strong_texture)
        comps = [
            c for c in comps
            if not (
                c.get("touch_image_border")
                and float(c.get("score", 0.0)) < best_texture_score * 0.75
            )
        ]
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

        comp_pad_ratio = min(float(pad_ratio), 0.05) if comp.get("source") == "texture" else float(pad_ratio)
        pad_x = int(round(Bw * comp_pad_ratio))
        pad_y = int(round(Bh * comp_pad_ratio))

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
            "foreground_ratio": float(comp["foreground_ratio"]),
            "bbox_area_ratio": float(comp["bbox_area_ratio"]),
            "dark_core_area_ratio": float(comp["dark_core_area_ratio"]),
            "touch_image_border": bool(comp["touch_image_border"]),
            "image_border_sides": list(comp.get("image_border_sides") or []),
            "image_edge_clipped": bool(comp.get("image_edge_clipped", False)),
            "confidence": float(comp["confidence"]),
            "detection_source": comp.get("source", "dark"),
            "is_valid_for_compensation": bool(comp["confidence"] >= 0.25),
        })

    results = _dedupe_mapped_coarse_results(results)

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
    n_angles=360,
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


def _largest_centered_component(mask_u8, center_xy):
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
    """Refine a coarse mask with GrabCut while keeping strict ROI constraints."""
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
            max(1, int(iterations)),
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
    dark_percentile=42,
    density_sigma=12,
    radial_target_alpha=0.52,
    n_angles=180,
    radial_mode="hybrid",
    recenter_iterations=1,
    recenter_min_shift_px=6.0,
    clip_bbox_local=None,
    clip_pad_ratio=0.05,
    edge_refine_method="hybrid",
    edge_refine_iterations=2,
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

    allowed = None
    if clip_bbox_local is not None:
        bx, by, bw, bh = [int(round(v)) for v in clip_bbox_local]
        pad_x = int(round(max(0, bw) * float(clip_pad_ratio)))
        pad_y = int(round(max(0, bh) * float(clip_pad_ratio)))
        allowed = np.zeros_like(mask_full, dtype=np.uint8)
        center = (int(round(bx + bw / 2.0)), int(round(by + bh / 2.0)))
        axes = (
            max(1, int(round(bw / 2.0 + pad_x))),
            max(1, int(round(bh / 2.0 + pad_y))),
        )
        cv2.ellipse(allowed, center, axes, 0, 0, 360, 255, thickness=-1)
        mask_full = cv2.bitwise_and(mask_full, allowed)

    edge_refine_method = str(edge_refine_method or "none").lower()
    edge_refine_success = False
    edge_refine_meta = {
        "edge_refine_method": edge_refine_method,
        "edge_refine_success": False,
        "edge_refine_reason": "disabled",
    }
    if edge_refine_method in ("grabcut", "hybrid"):
        center_for_refine = [float(cx * scale), float(cy * scale)]
        refined_mask, edge_refine_success, edge_refine_meta = _edge_refine_mask_grabcut(
            roi_gray,
            mask_full,
            center_for_refine,
            allowed_mask_u8=allowed,
            iterations=edge_refine_iterations,
        )
        mask_full = refined_mask
        edge_refine_meta["edge_refine_method"] = "grabcut"
        edge_refine_meta["edge_refine_success"] = bool(edge_refine_success)

    contour_mode = cv2.CHAIN_APPROX_NONE if edge_refine_success else cv2.CHAIN_APPROX_SIMPLE
    cnts, _ = cv2.findContours(mask_full, cv2.RETR_EXTERNAL, contour_mode)
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
    eps_ratio = 0.0012 if edge_refine_success else 0.0035
    eps = max(1.0 if edge_refine_success else 2.0, eps_ratio * cv2.arcLength(cnt, True))
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
        "refine_method": edge_refine_meta.get("edge_refine_method", edge_refine_method),
        "edge_refine_success": bool(edge_refine_success),
        "edge_refine_reason": edge_refine_meta.get("edge_refine_reason"),
        "edge_refine_area_ratio": edge_refine_meta.get("edge_refine_area_ratio"),
    }, {
        "flat": flat,
        "dark_seed": dark_seed_u8,
        "density": density_u8,
        "mask_small": mask_small,
        "scale": scale,
        "target": float(target),
        "center_history_small": center_history,
        **edge_refine_meta,
    }
