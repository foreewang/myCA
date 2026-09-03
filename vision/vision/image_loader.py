"""图像读取和格式标准化。

检测算法希望输入是 uint8 单通道灰度图。相机、磁盘文件或上层接口
传进来的图片可能是彩色图、带 alpha 通道的图，或者 16bit/float 图。
本模块负责把这些输入先统一成后续算法容易处理的格式。
"""

from pathlib import Path

import cv2
import numpy as np


def to_gray_u8(img):
    """把输入图像统一转换为 uint8 灰度图。

    初学者提示:
    - OpenCV 读取彩色图时默认通道顺序是 BGR，不是 RGB。
    - uint8 表示像素范围 0~255，很多 OpenCV 阈值和滤波流程都默认
      使用这个范围。
    - 这里不做增强、去噪或分割，只做“格式标准化”。
    """
    if img is None:
        raise ValueError("input image is None")

    if img.ndim == 3:
        if img.shape[2] == 4:
            # 带 alpha 通道时先丢掉透明度通道，再转灰度。
            img = img[:, :, :3]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        # 已经是单通道时复制一份，避免原数组被后续流程意外修改。
        gray = img.copy()

    if gray.dtype != np.uint8:
        gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

    return gray


def load_image(image_path, flags=cv2.IMREAD_UNCHANGED):
    """从磁盘读取图片，读取失败时抛出清晰异常。

    Windows 与 Linux 下部分 OpenCV 构建的 ``imread`` 都不能可靠处理非 ASCII 路径。
    先由 NumPy 读取原始字节，再交给 ``imdecode``，可以保持完整 Unicode
    路径，同时仍由 OpenCV 负责图片格式解码。
    """
    image_path = Path(image_path)
    try:
        encoded = np.fromfile(str(image_path), dtype=np.uint8)
    except OSError as exc:
        raise FileNotFoundError(f"cannot read image: {image_path}") from exc

    src = cv2.imdecode(encoded, flags) if encoded.size else None

    if src is None:
        raise FileNotFoundError(f"cannot read image: {image_path}")

    return src


def save_image(image_path, image, params=None):
    """以支持 Unicode 路径的方式保存 OpenCV 图像。"""
    image_path = Path(image_path)
    image_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = image_path.suffix or ".png"
    ok, encoded = cv2.imencode(suffix, image, params or [])
    if not ok:
        raise OSError(f"cannot encode image: {image_path}")
    try:
        encoded.tofile(str(image_path))
    except OSError as exc:
        raise OSError(f"cannot write image: {image_path}") from exc
    return str(image_path)


def load_gray_image(image_path):
    """读取图片并转换为 uint8 灰度图。"""
    src = load_image(image_path)
    return to_gray_u8(src)
