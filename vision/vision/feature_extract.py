"""把算法中间结果整理成统一 JSON 结构。

segment.py 更关注“怎么找到目标”，本文件更关注“怎么把找到的目标
转换成上层流程稳定可读的数据结构”。补偿流程、前端展示、调试脚本
都依赖这些字段名，所以这里尽量只做字段整理，不做复杂图像算法。
"""

import numpy as np


def _coarse_quality(coarse_item):
    """提取粗检测阶段的质量字段，并做最小限度的兜底。"""
    coarse_item = coarse_item or {}
    confidence = float(coarse_item.get("confidence", 0.0) or 0.0)
    confidence = max(0.0, min(1.0, confidence))
    return {
        "confidence": confidence,
        "is_valid_for_compensation": bool(coarse_item.get("is_valid_for_compensation", False)) and confidence >= 0.25,
        "foreground_ratio": coarse_item.get("foreground_ratio"),
        "bbox_area_ratio": coarse_item.get("bbox_area_ratio"),
        "dark_core_area_ratio": coarse_item.get("dark_core_area_ratio"),
        "dark_core_area_small": coarse_item.get("dark_core_area_small"),
        "touch_image_border": bool(coarse_item.get("touch_image_border", False)),
        "image_border_sides": list(coarse_item.get("image_border_sides") or []),
        "image_edge_clipped": bool(coarse_item.get("image_edge_clipped", coarse_item.get("touch_image_border", False))),
        "dark_core_center_pixel": coarse_item.get("dark_core_center_pixel"),
        "safe_point": coarse_item.get("safe_point") or coarse_item.get("dark_core_center_pixel"),
    }


def build_failed_component(idx, x, y, w, h, x0, y0, x1, y1, cx, cy, refine_debug, coarse_item=None):
    """生成“粗检测成功但轮廓细化失败”的结果结构。

    失败结果仍然保留 coarse_bbox、safe_point、质量字段等信息，
    方便人工复核和调试；但 is_valid_for_compensation 固定为 False，
    防止补偿流程使用不可靠目标。
    """
    quality = _coarse_quality(coarse_item)
    safe_point = quality.get("safe_point") or [int(cx), int(cy)]
    return {
        "id": f"C{idx:02d}",
        "coarse_bbox": [int(x), int(y), int(w), int(h)],
        "refine_roi_bbox": [int(x0), int(y0), int(x1 - x0), int(y1 - y0)],
        "center_pixel": [int(safe_point[0]), int(safe_point[1])],
        "contour_center_pixel": [int(cx), int(cy)],
        "dark_core_center_pixel": quality.get("dark_core_center_pixel"),
        "safe_point": [int(safe_point[0]), int(safe_point[1])],
        "bbox": [int(x), int(y), int(w), int(h)],
        "area_px": int(w * h),
        "contour_points": [],
        "center_history_small": refine_debug.get("center_history_small", []),
        "is_valid_for_compensation": False,
        "confidence": 0.0,
        "foreground_ratio": quality.get("foreground_ratio"),
        "bbox_area_ratio": quality.get("bbox_area_ratio"),
        "dark_core_area_ratio": quality.get("dark_core_area_ratio"),
        "dark_core_area_small": quality.get("dark_core_area_small"),
        "touch_image_border": quality.get("touch_image_border"),
        "image_border_sides": quality.get("image_border_sides"),
        "image_edge_clipped": quality.get("image_edge_clipped"),
    }


def build_refined_component(idx, x, y, w, h, x0, y0, x1, y1, refined_item, cnt_global, coarse_item=None):
    """生成轮廓细化成功后的标准目标结构。

    refined_item 中的坐标是 ROI 局部坐标，本函数会加上 ROI 左上角
    偏移量 (x0, y0)，把它们转换回整张原图坐标。
    refine_method/edge_refine_* 字段用于记录径向轮廓和 GrabCut 贴边的实际结果，
    方便现场复核某个克隆为什么贴边成功、失败或回退。
    """
    bx, by, bw, bh = refined_item["bbox_local"]
    cxl = refined_item["center_local"][0]
    cyl = refined_item["center_local"][1]
    cxg = int(cxl + x0)
    cyg = int(cyl + y0)

    contour_center = refined_item.get("contour_center_local") or refined_item.get("center_local")
    contour_center_pixel = [int(contour_center[0] + x0), int(contour_center[1] + y0)]
    area_px = int(refined_item["area_px"])

    quality = _coarse_quality(coarse_item)
    is_valid = bool(quality["is_valid_for_compensation"]) and area_px > 0
    confidence = float(quality["confidence"] if is_valid else 0.0)

    return {
        "id": f"C{idx:02d}",
        "coarse_bbox": [int(x), int(y), int(w), int(h)],
        "refine_roi_bbox": [int(x0), int(y0), int(x1 - x0), int(y1 - y0)],
        "center_pixel": [int(cxg), int(cyg)],
        "contour_center_pixel": contour_center_pixel,
        "dark_core_center_pixel": quality.get("dark_core_center_pixel"),
        "safe_point": [int(cxg), int(cyg)],
        "bbox": [int(bx + x0), int(by + y0), int(bw), int(bh)],
        "area_px": area_px,
        "contour_points": cnt_global[:, 0, :].astype(int).tolist(),
        "center_history_small": refined_item.get("center_history_small", []),
        "refine_method": refined_item.get("refine_method"),
        "edge_refine_success": bool(refined_item.get("edge_refine_success", False)),
        "edge_refine_reason": refined_item.get("edge_refine_reason"),
        "edge_refine_area_ratio": refined_item.get("edge_refine_area_ratio"),
        "is_valid_for_compensation": bool(is_valid),
        "confidence": confidence,
        "foreground_ratio": quality.get("foreground_ratio"),
        "bbox_area_ratio": quality.get("bbox_area_ratio"),
        "dark_core_area_ratio": quality.get("dark_core_area_ratio"),
        "dark_core_area_small": quality.get("dark_core_area_small"),
        "touch_image_border": quality.get("touch_image_border"),
        "image_border_sides": quality.get("image_border_sides"),
        "image_edge_clipped": quality.get("image_edge_clipped"),
    }


def to_global_contour(contour_local, x0, y0):
    """把 ROI 内的局部轮廓点转换为原图全局轮廓点。"""
    cnt_local = np.array(contour_local, dtype=np.int32).reshape(-1, 1, 2)
    cnt_global = cnt_local + np.array([[[x0, y0]]], dtype=np.int32)
    return cnt_local, cnt_global
