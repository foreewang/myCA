from pathlib import Path

import cv2
import numpy as np


def to_gray_u8(img):
    """将输入图像规范为 uint8 灰度图。"""
    if img is None:
        raise ValueError('input image is None')
    if img.ndim == 3:
        if img.shape[2] == 4:
            # RGBA 场景下丢弃 alpha，避免 cvtColor 通道不匹配。
            img = img[:, :, :3]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img.copy()
    if gray.dtype != np.uint8:
        # 对非 8bit 图像归一化到 0~255，统一后续处理输入范围。
        gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    return gray


def load_image(image_path, flags=cv2.IMREAD_UNCHANGED):
    """读取图像文件，读取失败时抛出明确异常。"""
    image_path = str(Path(image_path))
    src = cv2.imread(image_path, flags)
    if src is None:
        raise FileNotFoundError(f'cannot read image: {image_path}')
    return src


def load_gray_image(image_path):
    """读取图像并转换为 uint8 灰度图。"""
    src = load_image(image_path)
    return to_gray_u8(src)
