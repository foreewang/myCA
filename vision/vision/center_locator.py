import cv2


def contour_centroid_from_mask(mask_u8, fallback_center):
    """从二值 mask 最大连通轮廓计算质心；失败时回退到给定中心点。"""
    cnts, _ = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return [int(fallback_center[0]), int(fallback_center[1])]
    cnt = max(cnts, key=cv2.contourArea)
    M = cv2.moments(cnt)
    if M['m00'] > 1e-6:
        return [int(M['m10'] / M['m00']), int(M['m01'] / M['m00'])]
    return [int(fallback_center[0]), int(fallback_center[1])]
