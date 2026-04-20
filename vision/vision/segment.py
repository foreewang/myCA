import cv2
import numpy as np

from .preprocess import resize_keep_ratio, auto_seed_threshold, roi_density_signal
from .postprocess import nms_xywh, circular_smooth
from .center_locator import contour_centroid_from_mask


def detect_coarse_rois(gray, work_max=1024, flat_sigma=41, seed_thresh=None,
                       density_sigma=25, close_kernel=35, open_kernel=9,
                       min_area=10000, max_keep=None, pad_ratio=0.15,
                       nms_iou_thr=0.30, border_margin=2,
                       border_keep_min_area=50000):
    """
    在全图上做“粗检测”，输出候选 ROI 与粗中心点。

    核心思路:
    1) 缩放 + 平坦化，削弱光照不均；
    2) 提取暗种子并做密度平滑；
    3) 二值化 + 形态学，得到稳定连通域；
    4) 连通域筛选 + NMS，回映射到原图坐标。
    """
    small, scale = resize_keep_ratio(gray, work_max=work_max)

    # 背景平坦化: 让“相对更暗”的区域更突出。
    bg = cv2.GaussianBlur(small, (0, 0), flat_sigma)
    flat = cv2.normalize(
        cv2.divide(small.astype(np.float32), bg.astype(np.float32) + 1.0, scale=128.0),
        None, 0, 255, cv2.NORM_MINMAX
    ).astype(np.uint8)

    if seed_thresh is None:
        seed_thresh = auto_seed_threshold(flat)

    # 暗种子 -> 密度图: 把零散暗像素变成连续响应区域。
    dark_seed = (flat < seed_thresh).astype(np.uint8) * 255

    density = cv2.GaussianBlur((dark_seed > 0).astype(np.float32), (0, 0), density_sigma)
    density_u8 = cv2.normalize(density, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

    _, binary = cv2.threshold(density_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    binary = cv2.morphologyEx(
        binary, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_kernel, close_kernel))
    )
    binary = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (open_kernel, open_kernel))
    )

    n, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)
    Hs, Ws = small.shape
    comps = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < min_area:
            continue

        touch_border = (
            x <= border_margin or y <= border_margin or
            x + w >= Ws - border_margin or y + h >= Hs - border_margin
        )
        if touch_border and area < border_keep_min_area:
            continue

        cx, cy = centroids[i]
        comps.append({
            'label': int(i),
            'bbox_small': [int(x), int(y), int(w), int(h)],
            'area_small': int(area),
            'center_small': [float(cx), float(cy)],
            'score': float(area),
            'bbox': [int(x), int(y), int(w), int(h)],
            'touch_border': bool(touch_border),
        })

    # NMS 去重，避免多个候选框指向同一目标。
    comps = nms_xywh(comps, key_score='score', key_bbox='bbox', iou_thr=nms_iou_thr)
    comps.sort(key=lambda d: d['area_small'], reverse=True)
    if max_keep is not None and max_keep > 0:
        comps = comps[:max_keep]

    H, W = gray.shape
    results = []
    for comp in comps:
        x, y, w, h = comp['bbox_small']
        cx, cy = comp['center_small']

        X = int(round(x * scale))
        Y = int(round(y * scale))
        Bw = int(round(w * scale))
        Bh = int(round(h * scale))
        Cx = int(round(cx * scale))
        Cy = int(round(cy * scale))

        pad_x = int(round(Bw * pad_ratio))
        pad_y = int(round(Bh * pad_ratio))

        x0 = max(0, X - pad_x)
        y0 = max(0, Y - pad_y)
        x1 = min(W, X + Bw + pad_x)
        y1 = min(H, Y + Bh + pad_y)

        results.append({
            'coarse_bbox': [int(x0), int(y0), int(x1 - x0), int(y1 - y0)],
            'coarse_center_pixel': [int(Cx), int(Cy)],
            'area_small': int(comp['area_small']),
        })

    debug = {
        'small_gray': small,
        'flat': flat,
        'dark_seed': dark_seed,
        'density_u8': density_u8,
        'binary_small': binary,
        'scale': scale,
        'seed_thresh': int(seed_thresh),
    }
    return results, debug



def radial_contour_from_signal_vectorized(signal_u8, center_xy, n_angles=180,
                                          inner_ratio=0.12, border_strip=20,
                                          target_alpha=0.52, min_radius=10,
                                          mode='hybrid', grad_refine_window=18,
                                          fallback_radius_ratio=0.18):
    """
    在密度图上做向量化径向采样，估计闭合轮廓。

    mode:
    - threshold: 使用阈值穿越位置
    - gradient: 使用梯度最小(边界最陡下降)位置
    - hybrid: 先阈值定位，再局部梯度微调
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
    # 目标阈值位于“内部强度”和“外部强度”之间。
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

    profiles = signal_u8[ys, xs].astype(np.float32)

    # 对每条径向 profile 做 1D 平滑，减小噪声锯齿。
    kx = cv2.getGaussianKernel(ksize=9, sigma=2.0).astype(np.float32).reshape(-1)
    profiles = cv2.sepFilter2D(
        profiles, ddepth=-1,
        kernelX=kx,
        kernelY=np.array([1.0], dtype=np.float32),
        borderType=cv2.BORDER_REPLICATE
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

    if mode == 'threshold':
        radii = radii_thr
    else:
        grad = np.diff(profiles, axis=1)
        radii = radii_thr.copy()

        for i in range(n_angles):
            if not any_hit[i]:
                if mode == 'gradient':
                    j = int(np.argmin(grad[i]))
                    radii[i] = rs[min(j, rs.size - 1)]
                continue

            base_j = int(last_idx[i])
            if mode == 'gradient':
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



def refine_contour_in_roi(roi_gray, center_hint_local,
                          max_work=1200,
                          dark_percentile=42,
                          density_sigma=12,
                          radial_target_alpha=0.52,
                          n_angles=180,
                          radial_mode='hybrid',
                          recenter_iterations=1,
                          recenter_min_shift_px=6.0):
    """
    在单个 ROI 中细化轮廓。

    流程:
    1) 构建密度信号；
    2) 径向估计轮廓；
    3) 根据 mask 质心迭代重定位中心；
    4) 回到全分辨率并输出 contour/bbox/center。
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
    radii = None
    mask_small = None

    for _ in range(max(1, recenter_iterations + 1)):
        # 以当前中心做径向轮廓估计。
        pts, target, radii = radial_contour_from_signal_vectorized(
            density_u8,
            (cx, cy),
            n_angles=n_angles,
            target_alpha=radial_target_alpha,
            mode=radial_mode,
        )

        mask_small = np.zeros_like(density_u8, dtype=np.uint8)
        cv2.fillPoly(mask_small, [pts.reshape(-1, 1, 2)], 255)
        mask_small = cv2.morphologyEx(
            mask_small, cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
        )
        mask_small = cv2.morphologyEx(
            mask_small, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        )

        # 用当前 mask 质心更新中心，提升中心偏移情况下的稳定性。
        new_center = contour_centroid_from_mask(mask_small, [cx, cy])
        shift = float(np.hypot(new_center[0] - cx, new_center[1] - cy))
        cx, cy = float(new_center[0]), float(new_center[1])
        cx = float(np.clip(cx, 5, small.shape[1] - 6))
        cy = float(np.clip(cy, 5, small.shape[0] - 6))
        center_history.append([float(cx), float(cy)])

        if shift < recenter_min_shift_px:
            break

    if scale > 1.0:
        # 小图推理时，将 mask 回放到 ROI 原尺寸。
        mask_full = cv2.resize(mask_small, (roi_gray.shape[1], roi_gray.shape[0]), interpolation=cv2.INTER_NEAREST)
    else:
        mask_full = mask_small.copy()

    cnts, _ = cv2.findContours(mask_full, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None, {
            'flat': flat,
            'dark_seed': dark_seed_u8,
            'density': density_u8,
            'mask_small': mask_small,
            'scale': scale,
            'target': float(target) if target is not None else None,
            'center_history_small': center_history,
        }

    cnt = max(cnts, key=cv2.contourArea)
    eps = max(2.0, 0.0035 * cv2.arcLength(cnt, True))
    cnt = cv2.approxPolyDP(cnt, eps, True)

    M = cv2.moments(cnt)
    if M['m00'] > 1e-6:
        center_refined = [int(M['m10'] / M['m00']), int(M['m01'] / M['m00'])]
    else:
        center_refined = [int(center_hint_local[0]), int(center_hint_local[1])]

    x, y, w, h = cv2.boundingRect(cnt)

    return {
        'contour_local': cnt[:, 0, :].astype(int).tolist(),
        'center_local': center_refined,
        'bbox_local': [int(x), int(y), int(w), int(h)],
        'area_px': int(cv2.contourArea(cnt)),
        'mask_full': mask_full,
        'center_history_small': center_history,
    }, {
        'flat': flat,
        'dark_seed': dark_seed_u8,
        'density': density_u8,
        'mask_small': mask_small,
        'scale': scale,
        'target': float(target),
        'center_history_small': center_history,
    }
