"""视觉检测的预处理函数。

这些函数不直接给出最终克隆轮廓，而是把原始灰度图转换成更适合
后续检测的中间图，例如背景校正图、暗区种子图和暗区密度图。
"""

import cv2
import numpy as np


def resize_keep_ratio(gray, work_max=1024):
    """按最长边限制等比例缩小图片。

    粗检测不需要在完整大图上计算，先缩小可以明显降低计算量。
    返回的 scale 是原图到小图的整数缩放倍数，后续可用它把坐标
    从小图映射回原图。
    """
    h, w = gray.shape

    # 至少为 1，表示只缩小、不放大。
    scale = max(1, int(round(max(h, w) / float(work_max))))
    small = cv2.resize(gray, (w // scale, h // scale), interpolation=cv2.INTER_AREA)
    return small, scale


def auto_seed_threshold(flat_u8, q=0.22, hard_floor=40, hard_ceil=130):
    """自动估计“暗区种子”阈值。

    flat_u8 是背景校正后的灰度图。克隆核心通常比背景更暗，
    所以这里用低分位数作为暗区阈值，再用 hard_floor/hard_ceil
    防止极端图片把阈值推得过低或过高。
    """
    val = float(np.quantile(flat_u8, q))
    return int(np.clip(round(val), hard_floor, hard_ceil))


def roi_density_signal(
    roi_gray,
    max_work=1200,
    clahe_clip=2.0,
    flat_sigma=35,
    dark_percentile=42,
    density_sigma=12,
):
    """在单个 ROI 内构建“暗像素密度图”。

    返回值依次为:
    - small: 可能缩小后的 ROI 灰度图。
    - flat: 做过背景校正的 ROI。
    - dark_seed_u8: 暗像素种子二值图。
    - density_u8: 暗种子平滑后的连续密度图。
    - scale: ROI 原图到 small 的缩放比例。

    初学者可以把 density_u8 理解为“哪里更像克隆主体”的热力图，
    后面的径向轮廓搜索主要就是在这张图上工作。
    """
    h, w = roi_gray.shape

    scale = max(1.0, max(h, w) / float(max_work))
    if scale > 1.0:
        small = cv2.resize(
            roi_gray,
            (max(1, int(round(w / scale))), max(1, int(round(h / scale)))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        small = roi_gray.copy()

    # CLAHE 增强局部对比度，减弱局部光照不均带来的影响。
    clahe = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=(8, 8))
    g = clahe.apply(small)

    # 用大尺度高斯模糊估计背景，再做除法校正。
    bg = cv2.GaussianBlur(g, (0, 0), flat_sigma)
    flat = cv2.normalize(
        cv2.divide(g.astype(np.float32), bg.astype(np.float32) + 1.0, scale=128.0),
        None,
        0,
        255,
        cv2.NORM_MINMAX,
    ).astype(np.uint8)

    # 取相对更暗的一批像素作为种子，不要求这一步已经是完整分割。
    seed_thr = np.percentile(flat, dark_percentile)
    dark_seed = (flat < seed_thr).astype(np.float32)

    # 把离散暗种子平滑成连续密度场，轮廓搜索会更稳定。
    density = cv2.GaussianBlur(dark_seed, (0, 0), density_sigma)
    density_u8 = cv2.normalize(density, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

    return small, flat, (dark_seed * 255).astype(np.uint8), density_u8, scale
