from pathlib import Path
import json

import cv2
import numpy as np


def draw_cross(img, center, color=(0, 255, 0), size=30, thickness=4):
    """在图像上绘制十字中心标记。"""
    cx, cy = center
    cv2.line(img, (cx - size, cy), (cx + size, cy), color, thickness)
    cv2.line(img, (cx, cy - size), (cx, cy + size), color, thickness)


def bbox_iou_xywh(a, b):
    """计算两个 xywh 框的 IoU(交并比)。"""
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ax1, ay1 = ax0 + aw, ay0 + ah
    bx1, by1 = bx0 + bw, by0 + bh

    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    iw = max(0, ix1 - ix0)
    ih = max(0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0

    area_a = max(0, aw) * max(0, ah)
    area_b = max(0, bw) * max(0, bh)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return float(inter) / float(union)



def nms_xywh(items, key_score='score', key_bbox='bbox', iou_thr=0.35):
    """基于 IoU 的非极大值抑制，保留高分且重叠较小的框。"""
    if not items:
        return []
    order = sorted(items, key=lambda d: float(d.get(key_score, 0.0)), reverse=True)
    keep = []
    for cur in order:
        ok = True
        for kept in keep:
            if bbox_iou_xywh(cur[key_bbox], kept[key_bbox]) > iou_thr:
                ok = False
                break
        if ok:
            keep.append(cur)
    return keep



def circular_smooth(values, window=11):
    """对环状序列做滑动平均，首尾相连以避免边界断裂。"""
    values = np.asarray(values, np.float32)
    pad = window // 2
    ext = np.r_[values[-pad:], values, values[:pad]]
    kernel = np.ones(window, np.float32) / float(window)
    return np.convolve(ext, kernel, mode='same')[pad:-pad]



def upscale_to_original(im_small, target_shape):
    """将小图放大回目标尺寸(最近邻，适合 mask/标签图)。"""
    H, W = target_shape
    return cv2.resize(im_small, (W, H), interpolation=cv2.INTER_NEAREST)



def save_outputs(src_path, out_dir, gray, refined, debug):
    """保存中间可视化结果与最终 JSON 输出。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cv2.imwrite(str(out_dir / '01_gray.bmp'), gray)
    cv2.imwrite(str(out_dir / '02_coarse_flat.bmp'), upscale_to_original(debug['coarse_flat'], gray.shape))
    cv2.imwrite(str(out_dir / '03_coarse_binary.bmp'), upscale_to_original(debug['coarse_binary'], gray.shape))
    cv2.imwrite(str(out_dir / '04_refine_density.bmp'), debug['full_refine_density'])
    cv2.imwrite(str(out_dir / '05_contour_mask.bmp'), debug['contour_mask'])
    cv2.imwrite(str(out_dir / '06_overlay.bmp'), debug['overlay'])

    result_json = {
        'input_path': str(src_path),
        'input_size': {'width': int(gray.shape[1]), 'height': int(gray.shape[0])},
        'component_count': len(refined),
        'coarse_seed_thresh': int(debug.get('coarse_seed_thresh', -1)),
        'component_ids': [d['id'] for d in refined],
        'components': refined,
    }
    with open(out_dir / '07_result.json', 'w', encoding='utf-8') as f:
        json.dump(result_json, f, ensure_ascii=False, indent=2)
    return result_json
