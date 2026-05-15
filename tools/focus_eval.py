#autofocus评测脚本，计算图像的Laplacian方差和Tenengrad指标来评估图像的清晰度。

import cv2
import numpy as np
from pathlib import Path

paths = {
    "no_autofocus": r"C:\colony_system\data\focus_eval\focus_effect_d4_4x_001\D4_002_row00_col01.bmp",
    "with_autofocus": r"C:\colony_system\data\focus_eval\no_autofocus\nofocus_effect_d4_4x_001\D4_002_row00_col01.bmp",
}

def focus_scores(img):
    lap = cv2.Laplacian(img, cv2.CV_64F).var()
    gx = cv2.Sobel(img, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(img, cv2.CV_64F, 0, 1, ksize=3)
    tenengrad = np.mean(gx * gx + gy * gy)
    return lap, tenengrad

for name, p in paths.items():
    img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
    if img is None:
        print(name, "读取失败:", p)
        continue
    lap, ten = focus_scores(img)
    print(name)
    print("  path =", p)
    print("  size =", img.shape[1], "x", img.shape[0])
    print("  laplacian_var =", lap)
    print("  tenengrad =", ten)