"""视觉检测流水线入口。

本模块把底层算法串成完整流程:
1. 统一输入图片格式。
2. 在整图上做粗检测，找到候选 ROI。
3. 在每个 ROI 内细化轮廓。
4. 生成 overlay、mask、JSON 等输出。

workflow 层如果配置 entrypoint 为 ``vision.detect_pipeline:process_image``，
最终会走到这里。
"""

import cv2
import numpy as np

from .feature_extract import build_failed_component, build_refined_component, to_global_contour
from .image_loader import load_gray_image, to_gray_u8
from .postprocess import draw_cross, save_outputs
from .scorer import score_components_by_area
from .segment import detect_coarse_rois, refine_contour_in_roi
from .well_boundary import annotate_pickability_from_visual_well_border


def detect_and_refine(
    gray,
    coarse_work_max=1024,
    refine_pad_ratio=0.20,
    max_keep=None,
    radial_mode="hybrid",
    recenter_iterations=1,
    seed_thresh=None,
    seed_quantile=0.12,
    seed_hard_floor=35,
    seed_hard_ceil=105,
    core_density_min=80,
    min_foreground_ratio=0.025,
    max_foreground_ratio=0.80,
    min_dark_core_area_ratio=0.00001,
    max_dark_core_area_ratio=0.12,
    max_bbox_area_ratio=0.30,
    refine_clip_to_coarse_bbox=True,
    refine_clip_pad_ratio=0.05,
    reject_border_touch=False,
    mm_per_pixel=None,
    detect_well_border=True,
    well_border_margin_mm=0.0,
    well_border_margin_px=30.0,
):
    """执行“粗检测 + 局部轮廓细化”的核心流程。

    返回 refined 和 debug:
    - refined 是最终目标列表，会写入 07_result.json 的 components。
    - debug 保存中间图，供 save_outputs 生成调试图片和 overlay。
    """
    coarse, coarse_debug = detect_coarse_rois(
        gray,
        work_max=coarse_work_max,
        max_keep=max_keep,
        seed_thresh=seed_thresh,
        seed_quantile=seed_quantile,
        seed_hard_floor=seed_hard_floor,
        seed_hard_ceil=seed_hard_ceil,
        core_density_min=core_density_min,
        min_foreground_ratio=min_foreground_ratio,
        max_foreground_ratio=max_foreground_ratio,
        min_dark_core_area_ratio=min_dark_core_area_ratio,
        max_dark_core_area_ratio=max_dark_core_area_ratio,
        max_bbox_area_ratio=max_bbox_area_ratio,
        reject_border_touch=reject_border_touch,
    )

    H, W = gray.shape
    refined = []

    # 这些全图大小的图用于调试和可视化。每个 ROI 的结果会回填到这里。
    full_refine_density = np.zeros_like(gray, dtype=np.uint8)
    full_contour_mask = np.zeros_like(gray, dtype=np.uint8)
    overlay = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    for idx, item in enumerate(coarse, start=1):
        # coarse_bbox 和 safe_point 都是原图坐标。
        x, y, w, h = item["coarse_bbox"]
        cx, cy = item.get("safe_point") or item.get("dark_core_center_pixel") or item["coarse_center_pixel"]

        # 对粗框再扩边，避免粗检测框过紧导致后续轮廓被截断。
        pad_x = int(round(w * refine_pad_ratio))
        pad_y = int(round(h * refine_pad_ratio))
        x0 = max(0, x - pad_x)
        y0 = max(0, y - pad_y)
        x1 = min(W, x + w + pad_x)
        y1 = min(H, y + h + pad_y)

        roi = gray[y0:y1, x0:x1]
        center_local = [cx - x0, cy - y0]

        refined_item, refine_debug = refine_contour_in_roi(
            roi,
            center_local,
            radial_mode=radial_mode,
            recenter_iterations=recenter_iterations,
            clip_bbox_local=(
                [x - x0, y - y0, w, h]
                if refine_clip_to_coarse_bbox
                else None
            ),
            clip_pad_ratio=refine_clip_pad_ratio,
        )

        if refined_item is None:
            # 细化失败时仍输出粗检测结果，便于上层知道哪里失败了。
            cv2.rectangle(overlay, (x, y), (x + w, y + h), (0, 0, 255), 8)
            draw_cross(overlay, (cx, cy), size=28, thickness=4)
            refined.append(
                build_failed_component(
                    idx, x, y, w, h, x0, y0, x1, y1, cx, cy, refine_debug, item
                )
            )
            continue

        # 把 ROI 局部轮廓映射回原图坐标后绘制 overlay。
        _, cnt_global = to_global_contour(refined_item["contour_local"], x0, y0)
        cv2.drawContours(overlay, [cnt_global], -1, (0, 0, 255), 8)

        cxl = refined_item["center_local"][0]
        cyl = refined_item["center_local"][1]
        cxg = int(cxl + x0)
        cyg = int(cyl + y0)

        draw_cross(overlay, (cxg, cyg), size=28, thickness=4)
        cv2.putText(
            overlay,
            f"C{idx:02d}",
            (cxg + 20, cyg - 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )

        # 合成全图 mask。多个 ROI 重叠时取最大值，相当于保留任一前景。
        roi_mask = refined_item["mask_full"]
        full_contour_mask[y0:y1, x0:x1] = np.maximum(
            full_contour_mask[y0:y1, x0:x1],
            roi_mask,
        )

        # 把 ROI 内的密度图恢复到 ROI 原始尺寸，再贴回全图。
        density_full = cv2.resize(
            refine_debug["density"],
            (roi.shape[1], roi.shape[0]),
            interpolation=cv2.INTER_LINEAR,
        )
        full_refine_density[y0:y1, x0:x1] = np.maximum(
            full_refine_density[y0:y1, x0:x1],
            density_full,
        )

        refined.append(
            build_refined_component(
                idx, x, y, w, h, x0, y0, x1, y1, refined_item, cnt_global, item
            )
        )

    refined = score_components_by_area(refined)
    well_border_detection = annotate_pickability_from_visual_well_border(
        gray,
        refined,
        mm_per_pixel=mm_per_pixel,
        well_border_margin_mm=float(well_border_margin_mm or 0.0),
        well_border_margin_px=float(well_border_margin_px or 30.0),
        enabled=bool(detect_well_border),
    )

    debug = {
        "coarse_flat": coarse_debug["flat"],
        "coarse_binary": coarse_debug["binary_small"],
        "coarse_scale": coarse_debug["scale"],
        "coarse_seed_thresh": coarse_debug["seed_thresh"],
        "coarse_density_thresh": coarse_debug.get("density_thresh"),
        "coarse_candidate_count": coarse_debug.get("coarse_candidate_count", len(coarse)),
        "full_refine_density": full_refine_density,
        "overlay": overlay,
        "contour_mask": full_contour_mask,
        "well_border_detection": well_border_detection,
    }
    return refined, debug


def detect_from_gray(
    gray,
    src_path="in_memory",
    out_dir=None,
    coarse_work_max=1024,
    refine_pad_ratio=0.20,
    max_keep=None,
    radial_mode="hybrid",
    recenter_iterations=1,
    seed_thresh=None,
    seed_quantile=0.12,
    seed_hard_floor=35,
    seed_hard_ceil=105,
    core_density_min=80,
    min_foreground_ratio=0.025,
    max_foreground_ratio=0.80,
    min_dark_core_area_ratio=0.00001,
    max_dark_core_area_ratio=0.12,
    max_bbox_area_ratio=0.30,
    refine_clip_to_coarse_bbox=True,
    refine_clip_pad_ratio=0.05,
    reject_border_touch=False,
    scale_bar=None,
    mm_per_pixel=None,
    detect_well_border=True,
    well_border_margin_mm=0.0,
    well_border_margin_px=30.0,
):
    """从内存图片执行检测。

    out_dir 为 None 时只返回字典，不写任何图片文件；传入目录时会写出
    01_gray.bmp 到 07_result.json 等调试/结果文件。
    """
    gray = to_gray_u8(gray)

    refined, debug = detect_and_refine(
        gray,
        coarse_work_max=coarse_work_max,
        refine_pad_ratio=refine_pad_ratio,
        max_keep=max_keep,
        radial_mode=radial_mode,
        recenter_iterations=recenter_iterations,
        seed_thresh=seed_thresh,
        seed_quantile=seed_quantile,
        seed_hard_floor=seed_hard_floor,
        seed_hard_ceil=seed_hard_ceil,
        core_density_min=core_density_min,
        min_foreground_ratio=min_foreground_ratio,
        max_foreground_ratio=max_foreground_ratio,
        min_dark_core_area_ratio=min_dark_core_area_ratio,
        max_dark_core_area_ratio=max_dark_core_area_ratio,
        max_bbox_area_ratio=max_bbox_area_ratio,
        refine_clip_to_coarse_bbox=refine_clip_to_coarse_bbox,
        refine_clip_pad_ratio=refine_clip_pad_ratio,
        reject_border_touch=reject_border_touch,
        mm_per_pixel=mm_per_pixel if mm_per_pixel is not None else (scale_bar or {}).get("mm_per_pixel") if isinstance(scale_bar, dict) else None,
        detect_well_border=detect_well_border,
        well_border_margin_mm=well_border_margin_mm,
        well_border_margin_px=well_border_margin_px,
    )

    if out_dir is None:
        return {
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
            "scale_bar": None,
            "component_ids": [d["id"] for d in refined],
            "components": refined,
        }

    return save_outputs(src_path, out_dir, gray, refined, debug, scale_bar=scale_bar)


def detect_from_path(image_path, out_dir="outputs_5120_contour_refined_opt", **kwargs):
    """读取图片路径并执行检测。其他参数透传给 detect_from_gray。"""
    gray = load_gray_image(image_path)
    return detect_from_gray(gray=gray, src_path=image_path, out_dir=out_dir, **kwargs)


def process_image(image_path, **kwargs):
    """给 workflow 或外部脚本使用的轻量入口。

    如果调用方没有显式传 out_dir，默认只返回内存结果，不额外写盘。
    workflow 当前会传入自己的 out_dir，用来生成 overlay 和 07_result.json。
    """
    if "out_dir" not in kwargs:
        kwargs["out_dir"] = None
    return detect_from_path(image_path=image_path, **kwargs)
