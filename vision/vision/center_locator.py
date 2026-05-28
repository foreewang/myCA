"""中心点定位工具。

这里放的是非常小的几何辅助函数，主要服务于轮廓细化阶段。
OpenCV 中坐标通常写作 (x, y)，而 NumPy 数组索引是 [y, x]，
阅读代码时要特别注意这两个顺序的区别。
"""

import cv2


def contour_centroid_from_mask(mask_u8, fallback_center):
    """计算二值 mask 中最大外轮廓的质心。

    参数
    ----
    mask_u8:
        uint8 二值图。前景为非 0，背景为 0。
    fallback_center:
        兜底中心点，格式为 (x, y) 或 [x, y]。

    返回
    ----
    list[int]
        质心坐标 [cx, cy]。如果没有轮廓，或轮廓面积退化到无法
        计算质心，则返回 fallback_center 的整数形式。
    """
    # 只取最外层轮廓，避免内部孔洞影响主体定位。
    contours, _ = cv2.findContours(
        mask_u8,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    if not contours:
        return [int(fallback_center[0]), int(fallback_center[1])]

    # 当前场景默认一个 mask 里主要目标是面积最大的那块。
    main_contour = max(contours, key=cv2.contourArea)
    moments = cv2.moments(main_contour)

    # m00 可以理解为轮廓面积。面积太小时质心公式不稳定，直接兜底。
    if moments["m00"] <= 1e-6:
        return [int(fallback_center[0]), int(fallback_center[1])]

    cx = int(moments["m10"] / moments["m00"])
    cy = int(moments["m01"] / moments["m00"])
    return [cx, cy]
