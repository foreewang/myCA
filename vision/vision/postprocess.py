"""检测后的可视化、去重和结果落盘。

这里的函数大多不改变检测结果本身，而是负责:
- 在图片上画中心点、轮廓、比例尺。
- 对候选框做 NMS 去重。
- 保存调试图片和 07_result.json。
"""

from pathlib import Path
import json

import cv2
import numpy as np


def draw_cross(img, center, color=(0, 255, 0), size=50, thickness=8):
    """在图像上原地绘制十字中心点。

    color 使用 OpenCV 的 BGR 顺序，例如绿色是 (0, 255, 0)。
    """
    cx, cy = center
    cv2.line(img, (cx - size, cy), (cx + size, cy), color, thickness)
    cv2.line(img, (cx, cy - size), (cx, cy + size), color, thickness)


def _nice_scale_length_mm(target_mm):
    """把目标物理长度吸附到更适合显示的刻度值。"""
    if target_mm <= 0:
        return None
    candidates = [
        0.01, 0.02, 0.05,
        0.1, 0.2, 0.5,
        1.0, 2.0, 5.0,
        10.0, 20.0, 50.0,
    ]
    best = candidates[0]
    for value in candidates:
        if value <= target_mm:
            best = value
        else:
            break
    return best


def _format_scale_label(length_mm):
    """把毫米长度格式化成 overlay 上的比例尺文字。"""
    if length_mm < 1.0:
        return f"{int(round(length_mm * 1000.0))} um"
    if abs(length_mm - round(length_mm)) < 1e-6:
        return f"{int(round(length_mm))} mm"
    return f"{length_mm:g} mm"


def draw_scale_bar(img, scale_bar=None):
    """在 overlay 上绘制比例尺，并返回写入 JSON 的比例尺信息。

    scale_bar 需要包含 mm_per_pixel。可选字段包括:
    enabled、target_px、length_mm、margin_px、thickness、font_scale、
    font_thickness、label、position、color_bgr、outline_bgr。
    """
    if not scale_bar:
        return None
    if isinstance(scale_bar, dict) and not scale_bar.get("enabled", True):
        return None

    cfg = scale_bar if isinstance(scale_bar, dict) else {}
    mm_per_pixel = cfg.get("mm_per_pixel")
    if mm_per_pixel is None:
        return None
    try:
        mm_per_pixel = float(mm_per_pixel)
    except Exception:
        return None
    if mm_per_pixel <= 0:
        return None

    height, width = img.shape[:2]
    target_px = float(cfg.get("target_px") or width * 0.16)
    length_mm = cfg.get("length_mm")
    if length_mm is None:
        length_mm = _nice_scale_length_mm(target_px * mm_per_pixel)
    if length_mm is None:
        return None
    length_mm = float(length_mm)
    length_px = int(round(length_mm / mm_per_pixel))
    if length_px < 10:
        return None

    margin_px = int(cfg.get("margin_px") or max(40, round(min(width, height) * 0.025)))
    thickness = int(cfg.get("thickness") or max(4, round(min(width, height) * 0.0025)))
    font_scale = float(cfg.get("font_scale") or max(0.8, min(width, height) / 4200.0))
    font_thickness = int(cfg.get("font_thickness") or max(2, round(thickness * 0.45)))
    label = str(cfg.get("label") or _format_scale_label(length_mm))
    position = str(cfg.get("position") or "bottom_right")

    if position == "bottom_left":
        x0 = margin_px
        x1 = x0 + length_px
    else:
        x1 = width - margin_px
        x0 = x1 - length_px
    y = height - margin_px

    x0 = max(margin_px, int(x0))
    x1 = min(width - margin_px, int(x1))
    if x1 <= x0:
        return None

    color = tuple(int(v) for v in cfg.get("color_bgr", (255, 255, 255)))
    outline = tuple(int(v) for v in cfg.get("outline_bgr", (0, 0, 0)))

    # 先画黑色描边，再画白色主体，保证在亮/暗背景上都清晰。
    cv2.line(img, (x0, y), (x1, y), outline, thickness + 4, cv2.LINE_AA)
    cv2.line(img, (x0, y), (x1, y), color, thickness, cv2.LINE_AA)
    tick_h = max(thickness * 3, 16)
    for x in (x0, x1):
        cv2.line(img, (x, y - tick_h // 2), (x, y + tick_h // 2), outline, thickness + 4, cv2.LINE_AA)
        cv2.line(img, (x, y - tick_h // 2), (x, y + tick_h // 2), color, thickness, cv2.LINE_AA)

    text_size, baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, font_thickness)
    text_w, _ = text_size
    tx = int(round((x0 + x1 - text_w) / 2))
    ty = int(y - tick_h - max(10, baseline))
    cv2.putText(img, label, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, font_scale, outline, font_thickness + 4, cv2.LINE_AA)
    cv2.putText(img, label, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, font_thickness, cv2.LINE_AA)

    return {
        "length_mm": float(length_mm),
        "length_px": int(x1 - x0),
        "label": label,
        "mm_per_pixel": float(mm_per_pixel),
        "position": position,
    }


def bbox_iou_xywh(a, b):
    """计算两个 [x, y, w, h] 边界框的 IoU。"""
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


def nms_xywh(items, key_score="score", key_bbox="bbox", iou_thr=0.35):
    """对候选框做非极大值抑制，去掉高度重叠的重复候选。"""
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
    """对环形序列做平滑。

    径向轮廓的半径序列首尾相接，所以平滑时也要把首尾拼起来，
    避免 0 度附近出现断裂。
    """
    values = np.asarray(values, np.float32)
    pad = window // 2
    ext = np.r_[values[-pad:], values, values[:pad]]

    kernel = np.ones(window, np.float32) / float(window)
    return np.convolve(ext, kernel, mode="same")[pad:-pad]


def upscale_to_original(im_small, target_shape):
    """把小图恢复到目标尺寸。

    用最近邻插值是为了保持 mask/标签图的离散值，不引入中间灰度。
    """
    H, W = target_shape
    return cv2.resize(im_small, (W, H), interpolation=cv2.INTER_NEAREST)


def save_outputs(src_path, out_dir, gray, refined, debug, scale_bar=None):
    """保存调试图、overlay 和最终 JSON。

    固定输出文件:
    - 01_gray.bmp: 标准化后的灰度输入。
    - 02_coarse_flat.bmp: 粗检测背景校正图。
    - 03_coarse_binary.bmp: 粗检测二值候选图。
    - 04_refine_density.bmp: ROI 细化密度图合成到全图后的结果。
    - 05_contour_mask.bmp: 最终轮廓 mask。
    - 06_overlay.bmp: 原图叠加轮廓、中心点和可选比例尺。
    - 07_result.json: 结构化检测结果。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cv2.imwrite(str(out_dir / "01_gray.bmp"), gray)
    cv2.imwrite(str(out_dir / "02_coarse_flat.bmp"), upscale_to_original(debug["coarse_flat"], gray.shape))
    cv2.imwrite(str(out_dir / "03_coarse_binary.bmp"), upscale_to_original(debug["coarse_binary"], gray.shape))
    cv2.imwrite(str(out_dir / "04_refine_density.bmp"), debug["full_refine_density"])
    cv2.imwrite(str(out_dir / "05_contour_mask.bmp"), debug["contour_mask"])

    overlay = debug["overlay"].copy()
    scale_bar_info = draw_scale_bar(overlay, scale_bar)
    cv2.imwrite(str(out_dir / "06_overlay.bmp"), overlay)

    result_json = {
        "input_path": str(src_path),
        "input_size": {
            "width": int(gray.shape[1]),
            "height": int(gray.shape[0]),
        },
        "component_count": len(refined),
        "coarse_seed_thresh": int(debug.get("coarse_seed_thresh", -1)),
        "coarse_density_thresh": debug.get("coarse_density_thresh"),
        "coarse_candidate_count": int(debug.get("coarse_candidate_count", len(refined))),
        "well_border_detection": debug.get("well_border_detection"),
        "scale_bar": scale_bar_info,
        "component_ids": [d["id"] for d in refined],
        "components": refined,
    }

    with open(out_dir / "07_result.json", "w", encoding="utf-8") as f:
        json.dump(result_json, f, ensure_ascii=False, indent=2)

    return result_json
