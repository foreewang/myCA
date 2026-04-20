import cv2


def contour_centroid_from_mask(mask_u8, fallback_center):
    """
    计算二值掩膜主体轮廓的质心坐标。

    处理策略：
    1. 在 mask 中提取最外层轮廓；
    2. 选择面积最大的轮廓，视为目标主体；
    3. 基于图像矩计算该轮廓的质心；
    4. 若未找到有效轮廓，或矩退化导致无法计算质心，则回退到 fallback_center。

    参数
    ----
    mask_u8 : np.ndarray
        uint8 二值掩膜。前景为非零，背景为 0。
    fallback_center : Sequence[Number]
        兜底中心点，格式为 (x, y) 或 [x, y]。

    返回
    ----
    list[int]
        质心坐标 [cx, cy]。若计算失败，返回 fallback_center 的整数形式。

    说明
    ----
    该函数假设目标主体对应 mask 中面积最大的外轮廓。
    这一假设适用于“单主体、少噪声”的分割结果；若 mask 中存在多个大目标，
    需要在上游先做目标筛选，或在此处补充更严格的轮廓判定规则。
    """
    # 仅提取最外层轮廓，避免内部孔洞或嵌套结构干扰主体定位。
    contours, _ = cv2.findContours(
        mask_u8,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    # 没有可用轮廓时，直接使用调用方提供的兜底中心。
    if not contours:
        return [int(fallback_center[0]), int(fallback_center[1])]

    # 默认将最大外轮廓视为目标主体。
    main_contour = max(contours, key=cv2.contourArea)
    moments = cv2.moments(main_contour)

    # m00 为面积项。过小或为 0 时，质心公式会失效，因此回退。
    if moments["m00"] <= 1e-6:
        return [int(fallback_center[0]), int(fallback_center[1])]

    cx = int(moments["m10"] / moments["m00"])
    cy = int(moments["m01"] / moments["m00"])
    return [cx, cy]
