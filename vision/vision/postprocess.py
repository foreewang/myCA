from pathlib import Path
import json

import cv2
import numpy as np


def draw_cross(img, center, color=(0, 255, 0), size=50, thickness=8):
    """
    在图像上绘制十字标记，用于突出显示中心点或关键定位点。

    参数
    ----
    img : np.ndarray
        待绘制图像。该函数会直接在原图上修改。
    center : Sequence[int]
        十字中心点坐标，格式为 (cx, cy)。
    color : tuple[int, int, int], optional
        十字颜色，默认绿色。按 OpenCV 的 BGR 顺序传入。
    size : int, optional
        十字臂长度的一半。实际横线和竖线总长度均为 2 * size。
    thickness : int, optional
        线宽。

    说明
    ----
    该函数不返回新图，而是原地修改输入图像，适合用于调试可视化、
    结果叠加图生成和关键点检查。
    """
    cx, cy = center
    cv2.line(img, (cx - size, cy), (cx + size, cy), color, thickness)
    cv2.line(img, (cx, cy - size), (cx, cy + size), color, thickness)


def bbox_iou_xywh(a, b):
    """
    计算两个边界框的 IoU（Intersection over Union，交并比）。

    输入框格式统一为 [x, y, w, h]，其中：
    - x, y 为左上角坐标；
    - w, h 为宽和高。

    参数
    ----
    a, b : Sequence[Number]
        两个待比较的边界框，格式均为 [x, y, w, h]。

    返回
    ----
    float
        IoU 值，范围为 [0, 1]。
        若两个框无交集，或并集无效，则返回 0.0。

    说明
    ----
    该函数默认不对输入框做合法性修正，只在面积或并集异常时兜底返回 0。
    因此更推荐由上游保证 bbox 数据已基本有效。
    """
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ax1, ay1 = ax0 + aw, ay0 + ah
    bx1, by1 = bx0 + bw, by0 + bh

    # 计算相交区域
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    iw = max(0, ix1 - ix0)
    ih = max(0, iy1 - iy0)
    inter = iw * ih

    if inter <= 0:
        return 0.0

    # 计算并集面积
    area_a = max(0, aw) * max(0, ah)
    area_b = max(0, bw) * max(0, bh)
    union = area_a + area_b - inter

    if union <= 0:
        return 0.0

    return float(inter) / float(union)


def nms_xywh(items, key_score='score', key_bbox='bbox', iou_thr=0.35):
    """
    对候选目标执行基于 IoU 的非极大值抑制（NMS）。

    处理逻辑：
    1. 按得分从高到低排序；
    2. 依次取出当前最高分目标；
    3. 若其与已保留目标的 IoU 超过阈值，则丢弃；
    4. 否则保留。

    参数
    ----
    items : list[dict]
        候选目标列表。每个元素通常至少包含得分字段和框字段。
    key_score : str, optional
        目标得分字段名，默认使用 'score'。
    key_bbox : str, optional
        目标边界框字段名，默认使用 'bbox'，格式要求为 [x, y, w, h]。
    iou_thr : float, optional
        IoU 抑制阈值。超过该阈值则认为两个框重叠过大，低分框被抑制。

    返回
    ----
    list[dict]
        NMS 后保留下来的目标列表，顺序为按得分筛选后的保留顺序。

    说明
    ----
    这是一个简单、直观的 NMS 实现，适合当前候选目标数量不多的场景。
    若后续目标数明显增大，可再考虑向量化或更高效实现。
    """
    if not items:
        return []

    # 先按得分降序排列，优先保留高分目标
    order = sorted(items, key=lambda d: float(d.get(key_score, 0.0)), reverse=True)

    keep = []
    for cur in order:
        ok = True
        for kept in keep:
            # 当前框若与已保留框重叠过大，则认为是重复候选，直接抑制
            if bbox_iou_xywh(cur[key_bbox], kept[key_bbox]) > iou_thr:
                ok = False
                break
        if ok:
            keep.append(cur)

    return keep


def circular_smooth(values, window=11):
    """
    对环状一维序列做滑动平均平滑。

    参数
    ----
    values : array-like
        输入序列。常用于角度采样、径向采样等首尾相接的数据。
    window : int, optional
        平滑窗口大小。建议使用奇数，以保证对称性。

    返回
    ----
    np.ndarray
        平滑后的浮点数组，长度与输入一致。

    说明
    ----
    与普通一维平滑不同，这里将序列视为“环状”数据：
    首尾拼接后再卷积，避免在 0/末尾位置出现边界断裂。
    适合处理一圈轮廓半径、角向响应曲线等周期性序列。
    """
    values = np.asarray(values, np.float32)
    pad = window // 2

    # 将首尾拼接，模拟周期边界条件
    ext = np.r_[values[-pad:], values, values[:pad]]

    kernel = np.ones(window, np.float32) / float(window)
    return np.convolve(ext, kernel, mode='same')[pad:-pad]


def upscale_to_original(im_small, target_shape):
    """
    将小图恢复到目标尺寸。

    参数
    ----
    im_small : np.ndarray
        待放大的图像，通常为小尺寸 mask、标签图或中间处理结果。
    target_shape : tuple[int, int]
        目标尺寸，格式为 (H, W)。

    返回
    ----
    np.ndarray
        放大后的图像，尺寸与 target_shape 一致。

    说明
    ----
    这里固定使用最近邻插值，是因为该函数主要用于：
    - 二值 mask；
    - 标签图；
    - 分割结果可视化；
    这些数据不适合使用双线性或双三次插值，否则会引入伪灰度和边界混叠。
    """
    H, W = target_shape
    return cv2.resize(im_small, (W, H), interpolation=cv2.INTER_NEAREST)


def save_outputs(src_path, out_dir, gray, refined, debug):
    """
    保存中间处理结果、可视化结果和最终 JSON 输出。

    输出内容包括：
    - 灰度图；
    - 粗检测相关中间结果；
    - 细化密度图；
    - 轮廓掩膜；
    - 叠加可视化图；
    - 最终结果 JSON。

    参数
    ----
    src_path : str or Path
        原始输入图像路径。
    out_dir : str or Path
        输出目录。若不存在会自动创建。
    gray : np.ndarray
        原始输入对应的灰度图。
    refined : list[dict]
        最终目标结果列表。
    debug : dict
        调试信息字典，需包含中间图和部分调试字段。

    返回
    ----
    dict
        最终保存到 JSON 的结果字典，便于调用方继续复用而无需再次读盘。

    说明
    ----
    本函数既承担“结果落盘”，也承担“调试追踪”职责。
    统一在这里约定文件名，有利于后续批处理、自动评估和结果比对。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 保存中间结果图，便于排查粗检测、细化和轮廓恢复各阶段问题
    cv2.imwrite(str(out_dir / '01_gray.bmp'), gray)
    cv2.imwrite(str(out_dir / '02_coarse_flat.bmp'), upscale_to_original(debug['coarse_flat'], gray.shape))
    cv2.imwrite(str(out_dir / '03_coarse_binary.bmp'), upscale_to_original(debug['coarse_binary'], gray.shape))
    cv2.imwrite(str(out_dir / '04_refine_density.bmp'), debug['full_refine_density'])
    cv2.imwrite(str(out_dir / '05_contour_mask.bmp'), debug['contour_mask'])
    cv2.imwrite(str(out_dir / '06_overlay.bmp'), debug['overlay'])

    # 构建统一 JSON 输出结构，供上层流程、评估脚本或前端可视化直接读取
    result_json = {
        'input_path': str(src_path),
        'input_size': {
            'width': int(gray.shape[1]),
            'height': int(gray.shape[0]),
        },
        'component_count': len(refined),
        'coarse_seed_thresh': int(debug.get('coarse_seed_thresh', -1)),
        'component_ids': [d['id'] for d in refined],
        'components': refined,
    }

    with open(out_dir / '07_result.json', 'w', encoding='utf-8') as f:
        json.dump(result_json, f, ensure_ascii=False, indent=2)

    return result_json