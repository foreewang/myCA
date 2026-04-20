import numpy as np


def build_failed_component(idx, x, y, w, h, x0, y0, x1, y1, cx, cy, refine_debug):
    """构建细化失败时的兜底结构，便于上层统一处理与落盘。"""
    return {
        'id': f'C{idx:02d}',
        'coarse_bbox': [int(x), int(y), int(w), int(h)],
        'refine_roi_bbox': [int(x0), int(y0), int(x1 - x0), int(y1 - y0)],
        'center_pixel': [int(cx), int(cy)],
        'bbox': [int(x), int(y), int(w), int(h)],
        'area_px': int(w * h),
        'contour_points': [],
        'center_history_small': refine_debug.get('center_history_small', []),
    }



def build_refined_component(idx, x, y, w, h, x0, y0, x1, y1, refined_item, cnt_global):
    """把 ROI 局部细化结果映射回全图坐标系，并整理为统一输出格式。"""
    bx, by, bw, bh = refined_item['bbox_local']
    cxl = refined_item['center_local'][0]
    cyl = refined_item['center_local'][1]
    cxg = int(cxl + x0)
    cyg = int(cyl + y0)

    return {
        'id': f'C{idx:02d}',
        'coarse_bbox': [int(x), int(y), int(w), int(h)],
        'refine_roi_bbox': [int(x0), int(y0), int(x1 - x0), int(y1 - y0)],
        'center_pixel': [int(cxg), int(cyg)],
        'bbox': [int(bx + x0), int(by + y0), int(bw), int(bh)],
        'area_px': int(refined_item['area_px']),
        'contour_points': cnt_global[:, 0, :].astype(int).tolist(),
        'center_history_small': refined_item.get('center_history_small', []),
    }



def to_global_contour(contour_local, x0, y0):
    """将 ROI 内局部轮廓平移为全图轮廓。"""
    cnt_local = np.array(contour_local, dtype=np.int32).reshape(-1, 1, 2)
    cnt_global = cnt_local + np.array([[[x0, y0]]], dtype=np.int32)
    return cnt_local, cnt_global
