"""对焦评价与黄金分割搜索。

- compute_focus_metric: 根据图像计算清晰度（越大越清晰）。
- golden_section_search: 一维单峰函数最大值搜索，采样次数少。
- auto_focus: 结合电机与相机，在给定范围内搜索最佳对焦位置。
"""

# sqrt 用来计算黄金分割比例。
from math import sqrt
# Callable 表示“可以被调用的函数”；Tuple/Optional 用于类型标注。
from typing import Callable, Tuple, Optional

# OpenCV 用于图像灰度转换、缩放、Laplacian 边缘计算。
import cv2
# numpy 用于图像数组和分位数计算。
import numpy as np


def compute_focus_metric(
    # 输入图像，项目里统一使用 BGR numpy 数组。
    image: np.ndarray,
    *,
    # 只取中心区域算清晰度，避免边缘噪声或无关物体影响判断。
    center_roi: Optional[float] = 0.6,
    # 算清晰度前做下采样，降低灰尘和传感器噪声的影响。
    downsample: float = 0.5,
    # 灰度分位数裁剪范围，用来压制极亮或极暗的异常点。
    clip_percentile: Optional[Tuple[float, float]] = (1.0, 99.0),
) -> float:
    """
    清晰度指标（越大越清晰）。

    为避免“灰尘/划痕”这类细小高频边缘把焦点带偏，本函数默认做两类抑制：
    - center_roi: 只计算图像中心区域（0~1，表示边长比例），更偏向细胞所在区域
    - downsample: 先下采样再算 Laplacian 方差，削弱微小尘点对高频的支配
    - clip_percentile: 灰度分位数裁剪，降低极亮/极暗点的影响
    """
    # 如果没有图像，直接给 0 分，避免后续 OpenCV 报错。
    if image is None or image.size == 0:
        return 0.0

    # 彩色图先转灰度；清晰度评价只需要亮度变化，不需要颜色。
    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        # 如果已经是灰度图，复制一份，避免后续处理改到外部传入的数组。
        gray = image.copy()

    # 1) 取中心 ROI（更偏向细胞）
    if center_roi is not None:
        # 转成 float，允许 YAML 里写整数或字符串形式的数字。
        center_roi = float(center_roi)
        # 只有 0~1 之间才裁剪中心区域；0 或 1 表示基本用全图。
        if 0.0 < center_roi < 1.0:
            # 读取图像高度和宽度。
            h, w = gray.shape[:2]
            # 根据比例计算中心 ROI 的高度和宽度。
            ch = int(h * center_roi)
            cw = int(w * center_roi)
            # 计算中心 ROI 左上角坐标。
            y0 = max(0, (h - ch) // 2)
            x0 = max(0, (w - cw) // 2)
            # 从灰度图中裁剪出中心区域。
            gray = gray[y0 : y0 + ch, x0 : x0 + cw]

    # 2) 分位数裁剪，降低极端亮点/暗点（尘点反光等）
    if clip_percentile is not None:
        # 解包低分位和高分位，例如 1% 与 99%。
        lo_p, hi_p = clip_percentile
        # 计算低分位灰度值。
        lo = np.percentile(gray, lo_p)
        # 计算高分位灰度值。
        hi = np.percentile(gray, hi_p)
        # 只有高分位大于低分位时，归一化才有意义。
        if hi > lo:
            # 把极端亮点/暗点压到分位数边界内。
            gray = np.clip(gray, lo, hi)
            # 重新拉伸到 0~255，让 Laplacian 分数尺度更稳定。
            gray = ((gray - lo) * (255.0 / (hi - lo))).astype(np.uint8)
        else:
            # 极端情况下图像几乎全同色，直接转成 uint8。
            gray = gray.astype(np.uint8)

    # 3) 下采样，削弱微小高频（灰尘）对指标的支配
    # 转成 float，允许配置文件里写成字符串或整数。
    downsample = float(downsample)
    # 只有 0~1 之间才做缩小；大于等于 1 表示不缩小。
    if 0.0 < downsample < 1.0:
        # INTER_AREA 适合缩小图像，能减少噪点造成的假高频。
        gray = cv2.resize(gray, None, fx=downsample, fy=downsample, interpolation=cv2.INTER_AREA)

    # Laplacian 会突出边缘和细节；越清晰，二阶变化通常越大。
    lap = cv2.Laplacian(gray, cv2.CV_64F, ksize=3)
    # 方差越大，说明边缘变化越明显，作为清晰度分数返回。
    return float(lap.var())


def golden_section_search(
    f: Callable[[float], float],
    left: float,
    right: float,
    tol: float = 1.0,
    max_iter: int = 50,
) -> Tuple[float, float]:
    """
    在 [left, right] 内用黄金分割搜索最大值位置。
    f: 单峰函数，输入位置，返回清晰度（越大越好）。
    返回: (最佳位置, 该位置清晰度)。
    """
    # 黄金分割比例，用于用较少采样逐步缩小搜索区间。
    phi = (sqrt(5) - 1) / 2

    # 第一个内部采样点，靠近右侧。
    x1 = right - phi * (right - left)
    # 第二个内部采样点，靠近左侧。
    x2 = left + phi * (right - left)
    # 在 x1 位置移动电机、取图并算清晰度。
    f1 = f(x1)
    # 在 x2 位置移动电机、取图并算清晰度。
    f2 = f(x2)
    # 记录“实际采样过”的最高分位置，最后让电机回到这里。
    best_pos, best_val = (x1, f1) if f1 >= f2 else (x2, f2)

    # 最多迭代 max_iter 次，避免搜索时间过长。
    for _ in range(max_iter):
        # 当前区间已经小于容差时停止搜索。
        if abs(right - left) <= tol:
            break
        # 如果左内部点更清晰，最大值更可能在左半段。
        if f1 > f2:
            # 丢弃右侧区间。
            right = x2
            # 复用旧采样点，减少一次取图。
            x2, f2 = x1, f1
            # 计算新的左内部采样点。
            x1 = right - phi * (right - left)
            # 对新的位置取图并计算清晰度。
            f1 = f(x1)
            # 如果这次更清晰，就更新最佳采样点。
            if f1 > best_val:
                best_pos, best_val = x1, f1
        else:
            # 否则最大值更可能在右半段，丢弃左侧区间。
            left = x1
            # 复用旧采样点，减少一次取图。
            x1, f1 = x2, f2
            # 计算新的右内部采样点。
            x2 = left + phi * (right - left)
            # 对新的位置取图并计算清晰度。
            f2 = f(x2)
            # 如果这次更清晰，就更新最佳采样点。
            if f2 > best_val:
                best_pos, best_val = x2, f2

    # 返回实际测过的最高分位置，而不是仅返回最终区间中点。
    return best_pos, best_val


def auto_focus(
    move_and_capture: Callable[[float], float],
    motor_min: float,
    motor_max: float,
    tol: float = 1.0,
    max_iter: int = 50,
) -> Tuple[float, float]:
    """
    对焦入口：在 [motor_min, motor_max] 内搜索清晰度最大的位置。

    move_and_capture: 函数，输入电机位置，执行移动+采集+计算清晰度并返回清晰度。
    返回: (最佳位置, 该位置清晰度)。
    """
    # 包一层 focus_fun，让上层传来的函数签名更明确。
    def focus_fun(pos: float) -> float:
        # 搜索算法可能给出 numpy/float 等类型，这里统一转成 Python float。
        return move_and_capture(float(pos))

    # 调用一维黄金分割搜索，在软件限位范围内找最大清晰度。
    return golden_section_search(
        focus_fun,
        left=motor_min,
        right=motor_max,
        tol=tol,
        max_iter=max_iter,
    )
