import cv2
import numpy as np


def resize_keep_ratio(gray, work_max=1024):
    """按最长边等比缩小灰度图，返回缩放后图像与缩放倍数。"""
    h, w = gray.shape
    scale = max(1, int(round(max(h, w) / float(work_max))))
    small = cv2.resize(gray, (w // scale, h // scale), interpolation=cv2.INTER_AREA)
    return small, scale


def auto_seed_threshold(flat_u8, q=0.22, hard_floor=40, hard_ceil=130):
    """根据分位数自动估计暗区阈值，并限制在安全区间内。"""
    val = float(np.quantile(flat_u8, q))
    return int(np.clip(round(val), hard_floor, hard_ceil))


def roi_density_signal(roi_gray, max_work=1200, clahe_clip=2.0,
                       flat_sigma=35, dark_percentile=42, density_sigma=12):
    """
    在 ROI 内构建“暗像素密度信号”。

    返回:
    - small: 缩放后的 ROI
    - flat: 背景平坦化后的图
    - dark_seed_u8: 暗种子二值图(0/255)
    - density_u8: 暗种子经高斯平滑后的密度图(0~255)
    - scale: ROI 到 small 的缩放系数
    """
    h, w = roi_gray.shape
    scale = max(1.0, max(h, w) / float(max_work))
    if scale > 1.0:
        small = cv2.resize(
            roi_gray,
            (max(1, int(round(w / scale))), max(1, int(round(h / scale)))),
            interpolation=cv2.INTER_AREA
        )
    else:
        small = roi_gray.copy()

    # 先做局部对比度增强，再做背景平坦化，减小光照不均的影响。
    clahe = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=(8, 8))
    g = clahe.apply(small)
    bg = cv2.GaussianBlur(g, (0, 0), flat_sigma)
    flat = cv2.normalize(
        cv2.divide(g.astype(np.float32), bg.astype(np.float32) + 1.0, scale=128.0),
        None, 0, 255, cv2.NORM_MINMAX
    ).astype(np.uint8)

    # 使用百分位阈值得到“偏暗像素”种子。
    seed_thr = np.percentile(flat, dark_percentile)
    dark_seed = (flat < seed_thr).astype(np.float32)

    # 对暗种子做平滑，得到连续密度场，便于后续径向轮廓搜索。
    density = cv2.GaussianBlur(dark_seed, (0, 0), density_sigma)
    density_u8 = cv2.normalize(density, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    return small, flat, (dark_seed * 255).astype(np.uint8), density_u8, scale
