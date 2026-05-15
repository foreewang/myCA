# 计算两张图像之间的整图偏移，验证往复运动精度

import cv2
import numpy as np

img1 = cv2.imread(r"C:\colony_system\data\http_tests\24-well_pipeline_b2_detect_http_001\B2\images\B2_017_row02_col03.bmp", cv2.IMREAD_GRAYSCALE)
img2 = cv2.imread("image2.jpg", cv2.IMREAD_GRAYSCALE)

img1 = np.float32(img1)
img2 = np.float32(img2)

shift, response = cv2.phaseCorrelate(img1, img2)
dx, dy = shift

print(f"整图偏移: dx={dx:.2f}, dy={dy:.2f}")
print(f"response={response:.4f}")