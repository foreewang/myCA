import cv2
import numpy as np

from .image_loader import load_gray_image, to_gray_u8
from .segment import detect_coarse_rois, refine_contour_in_roi
from .postprocess import draw_cross, save_outputs
from .feature_extract import build_failed_component, build_refined_component, to_global_contour
from .scorer import score_components_by_area



def detect_and_refine(gray,
                      coarse_work_max=1024,
                      refine_pad_ratio=0.20,
                      max_keep=None,
                      radial_mode='hybrid',
                      recenter_iterations=1,
                      seed_thresh=None,
                      border_keep_min_area=50000):
    """
    主流程: 先粗检候选 ROI，再对每个 ROI 做轮廓细化。

    返回:
    - refined: 每个目标的结构化结果
    - debug: 关键中间图，供可视化和排障使用
    """
    coarse, coarse_debug = detect_coarse_rois(
        gray,
        work_max=coarse_work_max,
        max_keep=max_keep,
        seed_thresh=seed_thresh,
        border_keep_min_area=border_keep_min_area,
    )

    H, W = gray.shape
    refined = []
    full_refine_density = np.zeros_like(gray, dtype=np.uint8)
    full_contour_mask = np.zeros_like(gray, dtype=np.uint8)
    overlay = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    for idx, item in enumerate(coarse, start=1):
        # coarse_bbox 已经是全图坐标。
        x, y, w, h = item['coarse_bbox']
        cx, cy = item['coarse_center_pixel']

        # 二次扩边给 refine 留冗余，避免粗框偏紧导致轮廓截断。
        pad_x = int(round(w * refine_pad_ratio))
        pad_y = int(round(h * refine_pad_ratio))
        x0 = max(0, x - pad_x)
        y0 = max(0, y - pad_y)
        x1 = min(W, x + w + pad_x)
        y1 = min(H, y + h + pad_y)

        roi = gray[y0:y1, x0:x1]
        center_local = [cx - x0, cy - y0]

        # 在 ROI 内用径向方法细化轮廓与中心。
        refined_item, refine_debug = refine_contour_in_roi(
            roi, center_local,
            radial_mode=radial_mode,
            recenter_iterations=recenter_iterations,
        )

        if refined_item is None:
            # 细化失败时保留粗结果，便于回溯和后续人工检查。
            cv2.rectangle(overlay, (x, y), (x + w, y + h), (0, 0, 255), 8)
            draw_cross(overlay, (cx, cy), size=28, thickness=4)
            refined.append(build_failed_component(idx, x, y, w, h, x0, y0, x1, y1, cx, cy, refine_debug))
            continue

        _, cnt_global = to_global_contour(refined_item['contour_local'], x0, y0)

        cv2.drawContours(overlay, [cnt_global], -1, (0, 0, 255), 8)

        cxl = refined_item['center_local'][0]
        cyl = refined_item['center_local'][1]
        cxg = int(cxl + x0)
        cyg = int(cyl + y0)
        draw_cross(overlay, (cxg, cyg), size=28, thickness=4)
        cv2.putText(
            overlay, f'C{idx:02d}',
            (cxg + 20, cyg - 20),
            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2, cv2.LINE_AA,
        )

        # 将每个 ROI 的局部 mask 合成到全图 mask。
        roi_mask = refined_item['mask_full']
        full_contour_mask[y0:y1, x0:x1] = np.maximum(full_contour_mask[y0:y1, x0:x1], roi_mask)

        # 将每个 ROI 的密度图放回全图，便于查看细化依据。
        density_full = cv2.resize(
            refine_debug['density'],
            (roi.shape[1], roi.shape[0]),
            interpolation=cv2.INTER_LINEAR,
        )
        full_refine_density[y0:y1, x0:x1] = np.maximum(full_refine_density[y0:y1, x0:x1], density_full)

        refined.append(build_refined_component(idx, x, y, w, h, x0, y0, x1, y1, refined_item, cnt_global))

    refined = score_components_by_area(refined)
    debug = {
        'coarse_flat': coarse_debug['flat'],
        'coarse_binary': coarse_debug['binary_small'],
        'coarse_scale': coarse_debug['scale'],
        'coarse_seed_thresh': coarse_debug['seed_thresh'],
        'full_refine_density': full_refine_density,
        'overlay': overlay,
        'contour_mask': full_contour_mask,
    }
    return refined, debug



def detect_from_gray(gray,
                     src_path='in_memory',
                     out_dir=None,
                     coarse_work_max=1024,
                     refine_pad_ratio=0.20,
                     max_keep=None,
                     radial_mode='hybrid',
                     recenter_iterations=1,
                     seed_thresh=None,
                     border_keep_min_area=50000):
    """从内存中的图像数组执行检测；可选写盘输出。"""
    gray = to_gray_u8(gray)
    refined, debug = detect_and_refine(
        gray,
        coarse_work_max=coarse_work_max,
        refine_pad_ratio=refine_pad_ratio,
        max_keep=max_keep,
        radial_mode=radial_mode,
        recenter_iterations=recenter_iterations,
        seed_thresh=seed_thresh,
        border_keep_min_area=border_keep_min_area,
    )
    if out_dir is None:
        return {
            'input_path': str(src_path),
            'input_size': {'width': int(gray.shape[1]), 'height': int(gray.shape[0])},
            'component_count': len(refined),
            'coarse_seed_thresh': int(debug.get('coarse_seed_thresh', -1)),
            'component_ids': [d['id'] for d in refined],
            'components': refined,
        }
    return save_outputs(src_path, out_dir, gray, refined, debug)



def detect_from_path(image_path, out_dir='outputs_5120_contour_refined_opt', **kwargs):
    """从图像路径执行检测，返回 JSON 结果。"""
    gray = load_gray_image(image_path)
    return detect_from_gray(gray=gray, src_path=image_path, out_dir=out_dir, **kwargs)

def process_image(image_path, **kwargs):
    if "out_dir" not in kwargs:
        kwargs["out_dir"] = None
    return detect_from_path(image_path=image_path, **kwargs)