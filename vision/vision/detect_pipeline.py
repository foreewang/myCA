import cv2
import numpy as np

from .image_loader import load_gray_image, to_gray_u8
from .segment import detect_coarse_rois, refine_contour_in_roi
from .postprocess import draw_cross, save_outputs
from .feature_extract import build_failed_component, build_refined_component, to_global_contour
from .scorer import score_components_by_area


def detect_and_refine(
    gray,
    coarse_work_max=1024,
    refine_pad_ratio=0.20,
    max_keep=None,
    radial_mode='hybrid',
    recenter_iterations=1,
    seed_thresh=None,
    border_keep_min_area=50000,
):
    """
    执行“粗检测 + 局部细化”的两阶段目标检测流程。

    流程说明
    --------
    1. 先在整幅灰度图上进行粗检测，得到候选 ROI；
    2. 对每个候选 ROI 做二次扩边，避免粗框过紧截断目标；
    3. 在 ROI 内执行轮廓细化，获得更稳定的轮廓和中心点；
    4. 将局部结果映射回全图坐标系；
    5. 汇总生成结构化结果，同时输出调试图像。

    参数
    ----
    gray : np.ndarray
        输入灰度图，通常应为 uint8 单通道图像。
    coarse_work_max : int, optional
        粗检测阶段的最长边工作尺寸，用于控制粗检计算量。
    refine_pad_ratio : float, optional
        细化 ROI 的扩边比例。以粗框宽高为基准向四周扩展，避免目标边缘被截断。
    max_keep : int or None, optional
        粗检测阶段最多保留的候选数。None 表示不额外限制。
    radial_mode : str, optional
        ROI 轮廓细化所采用的径向模式，由下游 refine_contour_in_roi 解释。
    recenter_iterations : int, optional
        细化阶段中心点迭代更新次数。
    seed_thresh : int or None, optional
        粗检测暗种子阈值。None 表示由下游自动估计。
    border_keep_min_area : int, optional
        粗检测阶段边缘连通域保留的最小面积阈值。

    返回
    ----
    refined : list[dict]
        每个目标的结构化结果列表。
    debug : dict
        调试信息字典，包含粗检测中间结果、全图密度图、轮廓 mask、overlay 等。

    说明
    ----
    该函数是整个检测流程的主调度入口。
    它不关心某个细节算法如何实现，而是负责串联粗检、局部细化、
    坐标还原、可视化合成和结果汇总。
    """
    coarse, coarse_debug = detect_coarse_rois(
        gray,
        work_max=coarse_work_max,
        max_keep=max_keep,
        seed_thresh=seed_thresh,
        border_keep_min_area=border_keep_min_area,
    )

    H, W = gray.shape

    # refined: 汇总每个目标的最终结构化结果
    refined = []

    # 全图级调试图：
    # - full_refine_density: 所有 ROI 的局部密度图回填到全图后的结果
    # - full_contour_mask: 所有 ROI 的轮廓 mask 合成图
    # - overlay: 用于最终可视化叠加展示
    full_refine_density = np.zeros_like(gray, dtype=np.uint8)
    full_contour_mask = np.zeros_like(gray, dtype=np.uint8)
    overlay = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    for idx, item in enumerate(coarse, start=1):
        # coarse_bbox 和 coarse_center_pixel 已经是全图坐标
        x, y, w, h = item['coarse_bbox']
        cx, cy = item['coarse_center_pixel']

        # 对粗框进行二次扩边，给 refine 留出缓冲区：
        # 若粗框偏紧，直接 refine 容易把目标边界截断，导致轮廓不完整。
        pad_x = int(round(w * refine_pad_ratio))
        pad_y = int(round(h * refine_pad_ratio))

        x0 = max(0, x - pad_x)
        y0 = max(0, y - pad_y)
        x1 = min(W, x + w + pad_x)
        y1 = min(H, y + h + pad_y)

        # 截取当前候选 ROI
        roi = gray[y0:y1, x0:x1]

        # 将全图中心点转换到 ROI 局部坐标系中，供 refine 使用
        center_local = [cx - x0, cy - y0]

        # 在 ROI 内执行轮廓细化。
        # refined_item 为局部细化结果；
        # refine_debug 为局部调试信息，例如密度图、中心点迭代轨迹等。
        refined_item, refine_debug = refine_contour_in_roi(
            roi,
            center_local,
            radial_mode=radial_mode,
            recenter_iterations=recenter_iterations,
        )

        if refined_item is None:
            # 细化失败时，不直接丢弃该候选，而是保留粗检测结果作为兜底输出。
            # 这样做有两个好处：
            # 1. 上层流程仍然能拿到结构完整的结果；
            # 2. 便于后续人工复核和分析 refine 失败原因。
            cv2.rectangle(overlay, (x, y), (x + w, y + h), (0, 0, 255), 8)
            draw_cross(overlay, (cx, cy), size=28, thickness=4)

            refined.append(
                build_failed_component(
                    idx, x, y, w, h, x0, y0, x1, y1, cx, cy, refine_debug
                )
            )
            continue

        # 将局部轮廓映射回全图坐标系，便于统一绘制和输出
        _, cnt_global = to_global_contour(refined_item['contour_local'], x0, y0)

        # 在 overlay 上画全局轮廓
        cv2.drawContours(overlay, [cnt_global], -1, (0, 0, 255), 8)

        # 将局部中心点映射回全图坐标
        cxl = refined_item['center_local'][0]
        cyl = refined_item['center_local'][1]
        cxg = int(cxl + x0)
        cyg = int(cyl + y0)

        # 在 overlay 上绘制中心点和目标编号
        draw_cross(overlay, (cxg, cyg), size=28, thickness=4)
        cv2.putText(
            overlay,
            f'C{idx:02d}',
            (cxg + 20, cyg - 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )

        # 将当前 ROI 的局部 mask 合成到全图 mask 中。
        # 这里取 maximum，是为了兼容多个 ROI 对同一区域可能有覆盖的情况。
        roi_mask = refined_item['mask_full']
        full_contour_mask[y0:y1, x0:x1] = np.maximum(
            full_contour_mask[y0:y1, x0:x1],
            roi_mask,
        )

        # 将局部密度图插值恢复到 ROI 原尺寸，再回填到全图。
        # 这样可以在全图尺度下查看每个目标的细化依据。
        density_full = cv2.resize(
            refine_debug['density'],
            (roi.shape[1], roi.shape[0]),
            interpolation=cv2.INTER_LINEAR,
        )
        full_refine_density[y0:y1, x0:x1] = np.maximum(
            full_refine_density[y0:y1, x0:x1],
            density_full,
        )

        # 构建成功细化后的标准组件结构
        refined.append(
            build_refined_component(
                idx, x, y, w, h, x0, y0, x1, y1, refined_item, cnt_global
            )
        )

    # 对结果按面积进行评分和重排，统一输出顺序与 rank 逻辑
    refined = score_components_by_area(refined)

    # 汇总调试信息，供 save_outputs 或上层排障使用
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


def detect_from_gray(
    gray,
    src_path='in_memory',
    out_dir=None,
    coarse_work_max=1024,
    refine_pad_ratio=0.20,
    max_keep=None,
    radial_mode='hybrid',
    recenter_iterations=1,
    seed_thresh=None,
    border_keep_min_area=50000,
):
    """
    从内存中的图像数组执行检测流程，并按需要返回结果或落盘输出。

    参数
    ----
    gray : np.ndarray
        输入图像数组。允许传入非标准灰度图，函数内部会统一转为 uint8 灰度图。
    src_path : str, optional
        输入来源标识。若图像来自内存而非磁盘，可使用默认值。
    out_dir : str or Path or None, optional
        输出目录。若为 None，则仅返回结果字典而不写盘。
    其余参数 :
        直接透传给 detect_and_refine。

    返回
    ----
    dict
        当 out_dir 为 None 时，返回内存中的结果字典；
        否则返回 save_outputs 写盘后的结果字典。

    说明
    ----
    这是面向上层模块的“内存图像入口”。
    适合相机实时采集、批处理流水线中间结果、接口调用等不依赖磁盘路径的场景。
    """
    # 统一输入格式，确保后续主流程拿到的是标准 uint8 灰度图
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

    # 不写盘时，直接返回结构化结果
    if out_dir is None:
        return {
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

    # 需要写盘时，统一交给 save_outputs 落地中间结果和 JSON
    return save_outputs(src_path, out_dir, gray, refined, debug)


def detect_from_path(image_path, out_dir='outputs_5120_contour_refined_opt', **kwargs):
    """
    从图像路径读取输入并执行检测，返回最终结果字典。

    参数
    ----
    image_path : str or Path
        输入图像路径。
    out_dir : str or Path, optional
        结果输出目录，默认写入固定目录。
    **kwargs :
        其余参数透传给 detect_from_gray。

    返回
    ----
    dict
        最终检测结果字典。
    """
    gray = load_gray_image(image_path)
    return detect_from_gray(gray=gray, src_path=image_path, out_dir=out_dir, **kwargs)


def process_image(image_path, **kwargs):
    """
    为上层调用保留的轻量封装入口。

    约定：
    - 若调用方未显式提供 out_dir，则默认不写盘，仅返回内存结果；
    - 适合批处理脚本、服务接口或外部模块直接调用。

    参数
    ----
    image_path : str or Path
        输入图像路径。
    **kwargs :
        其他检测参数。

    返回
    ----
    dict
        检测结果字典。
    """
    if "out_dir" not in kwargs:
        kwargs["out_dir"] = None
    return detect_from_path(image_path=image_path, **kwargs)