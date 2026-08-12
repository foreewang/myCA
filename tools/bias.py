from pathlib import Path

import cv2
import numpy as np


def load_grayscale_image(path: Path) -> np.ndarray:
    """读取灰度图，并转换为 phaseCorrelate 要求的 float32 格式。"""
    if not path.is_file():
        raise FileNotFoundError(f"图片不存在: {path}")

    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise RuntimeError(f"OpenCV 无法读取图片，请检查文件格式或完整性: {path}")

    return image.astype(np.float32, copy=False)


def main() -> None:
    image_dir = Path(__file__).resolve().parent
    image1_path = image_dir / "Image_20260811145608163.bmp"
    image2_path = image_dir / "Image_20260811150324325.bmp"

    img1 = load_grayscale_image(image1_path)
    img2 = load_grayscale_image(image2_path)

    if img1.shape != img2.shape:
        raise ValueError(
            f"两张图片尺寸不一致，无法进行相位相关计算: {img1.shape} != {img2.shape}"
        )

    # 计算第二张图片相对于第一张图片的平移量。
    (dx, dy), response = cv2.phaseCorrelate(img1, img2)
    distance_px = np.hypot(dx, dy)

    print(f"dx = {dx:.4f} px")
    print(f"dy = {dy:.4f} px")
    print(f"response = {response:.6f}")
    print(f"总偏移 = {distance_px:.4f} px")


if __name__ == "__main__":
    main()
