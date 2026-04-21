import cv2
import numpy as np


def resize_keep_ratio(gray, work_max=1024):
    """
    按最长边约束对灰度图做等比例缩小。

    参数
    ----
    gray : np.ndarray
        输入灰度图，形状为 (H, W)。
    work_max : int, optional
        工作分辨率上限。缩放后图像的最长边会尽量接近但不超过该值。

    返回
    ----
    small : np.ndarray
        缩放后的灰度图。
    scale : int
        原图到缩放图的整数缩放倍数。原图尺寸约等于 small 尺寸乘以 scale。

    说明
    ----
    该函数的目标是降低后续粗检测或预处理阶段的计算成本，
    同时尽量保持原图宽高比不变。

    当前实现使用“整数倍缩放”策略：
    - scale >= 1；
    - 当原图本身不大时，scale 为 1，即不缩放；
    - 使用 INTER_AREA 插值，适合图像缩小时减少混叠。

    注意
    ----
    由于这里采用的是整数倍近似，缩放后尺寸不一定严格等于 work_max，
    但实现简单、稳定，且便于后续坐标换算。
    """
    h, w = gray.shape

    # 以最长边为基准估计缩放倍数，至少为 1，表示“不放大，只缩小”
    scale = max(1, int(round(max(h, w) / float(work_max))))

    # 保持宽高比，按整数倍缩小
    small = cv2.resize(gray, (w // scale, h // scale), interpolation=cv2.INTER_AREA)
    return small, scale


def auto_seed_threshold(flat_u8, q=0.22, hard_floor=40, hard_ceil=130):
    """
    基于分位数自动估计暗区种子阈值，并限制在预设安全范围内。

    参数
    ----
    flat_u8 : np.ndarray
        输入的平坦化灰度图，通常应为 uint8，像素范围约为 0~255。
    q : float, optional
        分位数位置。较小值会更偏向提取“更暗”的像素作为种子。
    hard_floor : int, optional
        阈值下限，避免因图像异常导致阈值过低。
    hard_ceil : int, optional
        阈值上限，避免因图像分布异常导致阈值过高。

    返回
    ----
    int
        约束后的整数阈值。

    说明
    ----
    该函数不是做全局最优阈值分割，而是估计“暗种子”的起始阈值。
    因此采用分位数比固定阈值更稳健，能够适应不同图像亮度基线；
    同时通过上下限裁剪，减少极端样本对阈值的破坏。
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
    """
    在 ROI 内构建“暗像素密度信号”，供后续轮廓细化或径向搜索使用。

    处理流程
    --------
    1. 对 ROI 按最大工作尺寸缩放，控制计算量；
    2. 使用 CLAHE 做局部对比度增强；
    3. 使用高斯模糊估计背景，并做背景平坦化；
    4. 通过百分位阈值提取“偏暗像素”种子；
    5. 对暗种子图做高斯平滑，得到连续密度场。

    参数
    ----
    roi_gray : np.ndarray
        输入 ROI 灰度图，形状为 (H, W)。
    max_work : int, optional
        ROI 处理时允许的最大工作尺寸。过大的 ROI 会先缩小后再处理。
    clahe_clip : float, optional
        CLAHE 对比度限制参数。值越大，局部对比度增强越强。
    flat_sigma : float, optional
        背景平坦化时高斯模糊的 sigma，用于估计低频背景。
    dark_percentile : float, optional
        暗种子提取所用的百分位阈值。值越小，选出的暗种子越保守。
    density_sigma : float, optional
        密度图平滑时使用的高斯 sigma。值越大，得到的密度场越平滑。

    返回
    ----
    small : np.ndarray
        缩放后的 ROI 灰度图。
    flat : np.ndarray
        背景平坦化后的灰度图，uint8。
    dark_seed_u8 : np.ndarray
        暗种子二值图，取值为 0 或 255。
    density_u8 : np.ndarray
        暗种子平滑后得到的密度图，范围归一化到 0~255。
    scale : float
        原 ROI 到 small 的缩放比例。
        当 scale > 1 时，表示原图被缩小；scale == 1 表示未缩放。

    说明
    ----
    该函数的核心目标不是直接给出最终分割结果，而是构造一个更平滑、
    更适合几何搜索的“暗目标响应场”。

    这对于边界破碎、纹理不均或局部亮度变化较强的克隆目标尤其有用，
    因为密度场通常比原始灰度或硬阈值二值图更稳定。
    """
    h, w = roi_gray.shape

    # 根据 ROI 尺寸决定是否缩小，以控制细化阶段的计算量
    scale = max(1.0, max(h, w) / float(max_work))
    if scale > 1.0:
        small = cv2.resize(
            roi_gray,
            (max(1, int(round(w / scale))), max(1, int(round(h / scale)))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        small = roi_gray.copy()

    # 先增强局部对比度，再估计低频背景并做平坦化，
    # 以减弱光照不均、背景渐变对暗目标提取的影响。
    clahe = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=(8, 8))
    g = clahe.apply(small)

    bg = cv2.GaussianBlur(g, (0, 0), flat_sigma)

    # 使用“原图 / 背景”的方式做背景平坦化，再归一化回 0~255
    flat = cv2.normalize(
        cv2.divide(g.astype(np.float32), bg.astype(np.float32) + 1.0, scale=128.0),
        None,
        0,
        255,
        cv2.NORM_MINMAX,
    ).astype(np.uint8)

    # 通过百分位阈值提取“相对偏暗”的像素作为种子。
    # 这里的目标不是精准二值分割，而是先给出暗区初始响应。
    seed_thr = np.percentile(flat, dark_percentile)
    dark_seed = (flat < seed_thr).astype(np.float32)

    # 对离散暗种子做高斯平滑，得到连续密度场，
    # 便于后续做径向轮廓搜索、峰值定位或区域扩展。
    density = cv2.GaussianBlur(dark_seed, (0, 0), density_sigma)
    density_u8 = cv2.normalize(density, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

    return small, flat, (dark_seed * 255).astype(np.uint8), density_u8, scale