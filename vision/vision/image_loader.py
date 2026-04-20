from pathlib import Path

import cv2
import numpy as np


def to_gray_u8(img):
    """
    将输入图像统一转换为 uint8 灰度图。格式标准化

    该函数用于为后续图像处理流程提供稳定、统一的输入格式：
    - 通道数统一为单通道灰度；
    - 数据类型统一为 uint8；
    - 像素值范围统一到 0~255。

    这样可以避免后续阈值分割、滤波、轮廓提取等步骤因输入图像
    通道格式或位深不一致而出现行为漂移。

    参数
    ----
    img : np.ndarray
        输入图像。支持灰度图、BGR 三通道图、带 alpha 的四通道图，
        以及非 uint8 类型图像。

    返回
    ----
    np.ndarray
        处理后的 uint8 单通道灰度图。

    异常
    ----
    ValueError
        当输入图像为 None 时抛出。

    说明
    ----
    1. 对于四通道图像，默认直接丢弃 alpha 通道；
    2. 对于非 uint8 图像，使用 min-max 归一化到 0~255；
    3. 该函数不负责去噪、增强或对比度调整，只做输入格式标准化。
    """
    if img is None:
        raise ValueError("input image is None")

    # 若为彩色图，统一转为单通道灰度图。
    if img.ndim == 3:
        if img.shape[2] == 4:
            # OpenCV 的 BGR2GRAY 期望 3 通道输入。
            # 对 RGBA / BGRA 图像先丢弃 alpha，避免通道数不匹配。
            img = img[:, :, :3]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        # 已经是单通道时，复制一份，避免上游数组被原地影响。
        gray = img.copy()

    # 后续算法默认以 uint8 灰度图作为输入。
    # 若输入不是 8bit，则归一化到 0~255，保证处理链输入范围一致。
    if gray.dtype != np.uint8:
        gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

    return gray


def load_image(image_path, flags=cv2.IMREAD_UNCHANGED):
    """
    从磁盘读取图像文件，读取失败时抛出明确异常。可靠读图

    相比直接调用 cv2.imread，本函数的目的主要是：
    - 统一路径处理方式；
    - 在读取失败时给出可追踪的异常，而不是静默返回 None；
    - 为上层模块提供一致的图像加载入口。

    参数
    ----
    image_path : str or Path
        图像文件路径。
    flags : int, optional
        OpenCV 读取标志，默认使用 cv2.IMREAD_UNCHANGED，
        即尽量保留原始通道数和位深信息。

    返回
    ----
    np.ndarray
        读取到的原始图像数组。

    异常
    ----
    FileNotFoundError
        当图像文件无法读取时抛出。
    """
    image_path = str(Path(image_path))
    src = cv2.imread(image_path, flags)

    if src is None:
        raise FileNotFoundError(f"cannot read image: {image_path}")

    return src


def load_gray_image(image_path):
    """
    读取图像并转换为 uint8 灰度图。

    这是一个面向上层业务流程的便捷入口，适用于大多数仅关心
    灰度处理链的场景，例如阈值分割、边缘提取、轮廓检测等。

    参数
    ----
    image_path : str or Path
        图像文件路径。

    返回
    ----
    np.ndarray
        uint8 单通道灰度图。
    """
    src = load_image(image_path)
    return to_gray_u8(src)