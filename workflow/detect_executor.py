"""
对扫描采集结果里的每一张图执行克隆检测，并把检测结果重新组织成“图像级 + 克隆级”的结构化输出。
它的输入是
已经完成采集后的 scan_result，
它的输出是
“这次扫描一共拍了哪些图、每张图识别到多少个克隆、每个克隆在图像中的位置，以及这张图对应的位移台位置”等信息。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image

from workflow.detect_api import run_detect_on_image


def _image_size(image_path: str) -> tuple[int, int]:
    """
    读取图像尺寸。

    参数
    ----
    image_path : str
        图像文件路径。

    返回
    ----
    tuple[int, int]
        图像宽高，格式为 (width, height)。

    说明
    ----
    这里单独封装尺寸读取，是为了避免主流程里直接操作 PIL，
    也方便后续若更换读取库时只改这一处。
    """
    with Image.open(image_path) as im:
        return int(im.width), int(im.height)


def _actual_stage_xy(capture: Dict[str, Any]) -> tuple[int | None, int | None]:
    """
    从单次采集记录中提取位移台实际到位坐标。

    参数
    ----
    capture : Dict[str, Any]
        单张图对应的采集记录，通常来自 scan_result["captures"] 中的一个元素。

    返回
    ----
    tuple[int | None, int | None]
        实际位移台坐标 (x, y)。
        若记录不存在、字段缺失或转换失败，则返回 None。

    说明
    ----
    这里读取的是 motion_result.after.x.current_pos / y.current_pos，
    表示电机执行移动后的实际位置，而不是计划目标位置。
    这对后续做“图像中心偏移 -> 世界坐标回传”很重要，因为实际位置
    比目标位置更能反映真实执行误差。
    """
    motion_result = capture.get("motion_result", {}) or {}
    after = motion_result.get("after", {}) or {}

    x = (((after.get("x") or {}).get("current_pos")))
    y = (((after.get("y") or {}).get("current_pos")))

    try:
        x = int(x) if x is not None else None
    except Exception:
        x = None

    try:
        y = int(y) if y is not None else None
    except Exception:
        y = None

    return x, y


def _offset_from_center(center_px: List[int], image_center_px: List[int]) -> List[int]:
    """
    计算目标中心点相对图像中心的像素偏移。

    参数
    ----
    center_px : List[int]
        目标中心点坐标 [cx, cy]。
    image_center_px : List[int]
        图像中心点坐标 [ix, iy]。

    返回
    ----
    List[int]
        相对图像中心的偏移 [dx, dy]，其中：
        dx = cx - ix
        dy = cy - iy

    说明
    ----
    该偏移量是后续做“图像坐标 -> 位移台补偿坐标”转换的关键中间量。
    """
    return [int(center_px[0] - image_center_px[0]), int(center_px[1] - image_center_px[1])]


def execute_detect_on_scan_result(ctx: Dict[str, Any], params: Dict[str, Any], scan_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    对一次扫描采集结果中的所有图像执行克隆检测，并汇总输出。

    参数
    ----
    ctx : Dict[str, Any]
        运行时上下文。当前主要从 task.detect 中读取检测配置。
    params : Dict[str, Any]
        当前任务参数，包含 task_id、well_name、plate_type、输出路径等信息。
    scan_result : Dict[str, Any]
        扫描采集结果。通常来自 scan_executor，包含 captures、scan_config、reference 等字段。

    返回
    ----
    Dict[str, Any]
        检测结果汇总字典，包含：
        - 每张图的路径、尺寸、位移台坐标
        - 每张图中的克隆列表
        - 总图像数和总克隆数
        - 可选写盘后的 JSON 结果

    处理流程
    --------
    1. 从扫描结果中读取视野尺寸（fov_mm）；
    2. 遍历每一张采集图像；
    3. 计算图像中心和 mm_per_pixel；
    4. 调用 detect_api 对当前图像执行检测；
    5. 将每个组件整理为 clone_out 结构；
    6. 汇总成 image 级结果；
    7. 生成整个任务的检测结果总表；
    8. 若指定输出路径，则写入 JSON。

    说明
    ----
    这个函数的职责是“把单图检测结果与扫描上下文重新绑定”。
    也就是说，它不只是调用检测模型，还负责把：
    - 图像路径
    - 图像尺寸
    - 图像中心
    - 视野物理尺寸
    - 位移台坐标
    - 检测组件
    这些信息统一整合起来，形成后续坐标回传和挑取流程可直接使用的数据结构。
    """
    detect_cfg = ctx["task"].get("detect", {}) or {}
    entrypoint = detect_cfg.get("entrypoint")

    # 从扫描配置中读取当前图像对应的物理视野尺寸。
    # 这决定了像素与物理距离的换算关系。
    fov_cfg = scan_result.get("scan_config", {}).get("fov_mm", {}) or {}
    fov_w_mm = float(fov_cfg.get("width"))
    fov_h_mm = float(fov_cfg.get("height"))

    images: List[Dict[str, Any]] = []

    for capture in scan_result.get("captures", []):
        image_path = capture.get("capture_result", {}).get("saved_path")
        if not image_path:
            continue

        # 读取图像宽高，并据此计算图像中心点。
        width, height = _image_size(image_path)
        image_center = [width // 2, height // 2]

        # 根据视野尺寸与图像分辨率，计算每个像素对应的物理尺寸。
        # 后续若要把像素偏移映射为位移台补偿量，需要依赖这个比例。
        mm_per_pixel = {
            "x": fov_w_mm / float(width),
            "y": fov_h_mm / float(height),
        }

        # 读取当前图像采集时位移台的实际到位坐标。
        actual_x, actual_y = _actual_stage_xy(capture)

        # 调用检测接口，对当前图像执行克隆识别。
        detect_result = run_detect_on_image(image_path, entrypoint=entrypoint)

        clones: List[Dict[str, Any]] = []
        for i, comp in enumerate(detect_result.get("components", []), start=1):
            center_px = [int(comp["center_pixel"][0]), int(comp["center_pixel"][1])]
            offset_px = _offset_from_center(center_px, image_center)

            # 这里构建的是“单个克隆”的标准输出结构。
            # 当前保留的是像素坐标、bbox、面积以及图像对应的位移台位置。
            # 若后续要做世界坐标回传，可以在这里继续追加 offset_mm、stage_target 等字段。
            clone_out = {
                "clone_id": comp.get("id", f"C{i:02d}"),
                "center_px": center_px,
                "offset_from_image_center_px": offset_px,
                "bbox": comp.get("bbox"),
                "area_px": comp.get("area_px"),
                "source_image_path": image_path,
                "stage_x_actual": actual_x,
                "stage_y_actual": actual_y,
            }
            clones.append(clone_out)

        # 构建“单张图像”的结果结构。
        # 该层级同时保留了扫描点信息、图像物理换算关系和图中克隆列表。
        images.append(
            {
                "index": int(capture["index"]),
                "row_index": int(capture["row_index"]),
                "col_index": int(capture["col_index"]),
                "image_path": image_path,
                "stage_x_target": int(capture["stage_x_target"]),
                "stage_y_target": int(capture["stage_y_target"]),
                "stage_x_actual": actual_x,
                "stage_y_actual": actual_y,
                "image_width_px": width,
                "image_height_px": height,
                "image_center_px": image_center,
                "mm_per_pixel": mm_per_pixel,
                "clone_count": int(detect_result.get("component_count", 0)),
                "clones": clones,
            }
        )

    total_clones = sum(int(x["clone_count"]) for x in images)

    # 构建任务级总结果。
    result = {
        "task_id": params["task_id"],
        "status": "success",
        "task_type": "single_well_scan_and_detect",
        "plate_type": params["plate_type"],
        "well_name": params["well_name"],
        "objective_name": params["objective_name"],
        "reference": scan_result.get("reference"),
        "scan_config": scan_result.get("scan_config"),
        "scan_result_json": params.get("scan_result_json"),
        "image_count": len(images),
        "total_clone_count": total_clones,
        "images": images,
    }

    # 若配置了检测结果输出路径，则将结果写入 JSON 文件，
    # 供后续坐标换算、排序、挑取或人工检查直接读取。
    output_json = params.get("detect_output_json")
    if output_json:
        out_path = Path(output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    return result