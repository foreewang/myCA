import numpy as np


def build_failed_component(idx, x, y, w, h, x0, y0, x1, y1, cx, cy, refine_debug):
    """
    构建细化阶段失败时的目标结果字典。

    当某个候选目标在 refine 阶段未能得到有效轮廓或有效分割结果时，
    仍然返回一个字段结构完整的结果对象，便于上层流程统一处理、
    统一落盘，以及避免因字段缺失导致后续可视化、排序或导出逻辑报错。

    参数
    ----
    idx : int
        目标序号，用于生成组件 ID。
    x, y, w, h : int or float
        粗检测阶段得到的目标框，格式为左上角坐标 + 宽高。
    x0, y0, x1, y1 : int or float
        细化阶段所使用 ROI 的边界范围，采用左上-右下坐标表示。
    cx, cy : int or float
        当前目标的中心点坐标。通常来自粗检测中心、历史中心，
        或 refine 失败时最后一次可用中心。
    refine_debug : dict
        细化阶段的调试信息字典。当前主要使用其中的
        'center_history_small' 字段记录中心点迭代历史。

    返回
    ----
    dict
        与正常组件结构兼容的失败兜底结果。该结果会显式标记：
        - contour_points 为空；
        - area_px 使用粗框面积近似代替；
        - center_history_small 保留调试轨迹，便于后续排查 refine 失败原因。

    说明
    ----
    这里返回“结构完整但内容降级”的组件信息，而不是返回 None。
    这样可以保证上层调用方不需要为失败样本单独分支处理，只需按统一
    数据结构读取即可。
    """
    return {
        # 统一组件编号格式，例如 C01、C02、C03
        'id': f'C{idx:02d}',

        # 粗检测阶段得到的原始候选框，便于追溯初始检测结果
        'coarse_bbox': [int(x), int(y), int(w), int(h)],

        # 本次 refine 所使用的 ROI 区域，格式仍保持为 [x, y, w, h]
        # 虽然输入是 (x0, y0, x1, y1)，这里统一转换为宽高形式，便于前后处理一致
        'refine_roi_bbox': [int(x0), int(y0), int(x1 - x0), int(y1 - y0)],

        # 当前可用的中心点坐标。即使 refine 失败，也保留一个可用中心供上层使用
        'center_pixel': [int(cx), int(cy)],

        # 兼容上层统一读取逻辑：失败时 bbox 直接退回粗检测框
        'bbox': [int(x), int(y), int(w), int(h)],

        # 失败时没有可靠分割面积，因此退化为使用粗框面积作为近似值
        'area_px': int(w * h),

        # 细化失败意味着没有可靠轮廓点，显式返回空列表
        'contour_points': [],

        # 保留小图尺度下的中心点迭代历史，用于调试 refine 过程是否漂移或发散
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
