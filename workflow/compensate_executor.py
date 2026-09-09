"""根据检测到的克隆位置计算位移补偿目标，并执行拍照复核的闭环补偿流程。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from PIL import Image

from workflow.camera_executor import capture_single_image
from workflow.detect_api import run_detect_on_image, rule_texture_kwargs
from workflow.file_io import atomic_write_json
from workflow.plate_geometry import get_axis_pulses_per_mm, get_view_signs
from workflow.platform_defaults import DEFAULT_MODBUS_PORT, DEFAULT_SCAN_SETTLE_S, apply_motion_profile_defaults
from workflow.stage_executor import move_to_absolute_with_approach
from workflow.task_control import raise_if_cancel_requested, report_progress


def _image_center_distance2(image_item: Dict[str, Any], clone_item: Dict[str, Any]) -> float:
    dx, dy = clone_item.get("offset_from_image_center_px", [0, 0])
    return float(dx * dx + dy * dy)


def _all_clone_refs(detect_result: Dict[str, Any]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    refs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for image_item in detect_result.get("images", []):
        for clone_item in image_item.get("clones", []):
            if clone_item.get("is_pickable") is not True:
                continue
            refs.append((image_item, clone_item))
    return refs


def _all_centering_refs(detect_result: Dict[str, Any]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    refs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for image_item in detect_result.get("images", []):
        for clone_item in image_item.get("clones", []):
            if clone_item.get("eligible_for_10x_centering") is True:
                refs.append((image_item, clone_item))
    return refs


def select_clone_for_compensation(
    detect_result: Dict[str, Any],
    selector_cfg: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    从 detect_result 中选择一个克隆作为补偿目标。

    支持模式：
    - first: 第一张图第一个克隆
    - largest_area: 所有图中面积最大的克隆
    - nearest_image_center: 所有图中距离图像中心最近的克隆
    - clone_id: 按 clone_id 匹配；可选 image_index 进一步限定
    - image_and_clone: 显式指定 image_index + clone_id
    """
    mode = str((selector_cfg or {}).get("mode") or "first").strip().lower()
    purpose = str((selector_cfg or {}).get("purpose") or "pick").strip().lower()
    if purpose not in {"pick", "10x_centering"}:
        raise ValueError("selector.purpose 必须是 pick 或 10x_centering")
    refs = _all_centering_refs(detect_result) if purpose == "10x_centering" else _all_clone_refs(detect_result)
    if not refs:
        requirement = "eligible_for_10x_centering=true" if purpose == "10x_centering" else "is_pickable=true"
        raise ValueError(f"detect_result 中没有 {requirement} 的克隆")

    if mode == "first":
        return refs[0]

    if mode == "largest_area":
        return max(refs, key=lambda t: float((t[1].get("area_px") or 0)))

    if mode == "nearest_image_center":
        return min(refs, key=lambda t: _image_center_distance2(t[0], t[1]))

    if mode == "clone_id":
        clone_id = str(selector_cfg.get("clone_id") or "").strip()
        image_index = selector_cfg.get("image_index")
        for image_item, clone_item in refs:
            if clone_item.get("clone_id") != clone_id:
                continue
            if image_index is not None and int(image_item.get("index")) != int(image_index):
                continue
            return image_item, clone_item
        raise ValueError(f"未找到 clone_id={clone_id!r} 对应的可挑取克隆")

    if mode == "image_and_clone":
        image_index = int(selector_cfg["image_index"])
        clone_id = str(selector_cfg["clone_id"])
        for image_item, clone_item in refs:
            if int(image_item.get("index")) == image_index and clone_item.get("clone_id") == clone_id:
                return image_item, clone_item
        raise ValueError(f"未找到 image_index={image_index}, clone_id={clone_id!r} 对应的可挑取克隆")

    raise ValueError(f"不支持的 compensate.selector.mode: {mode}")


def _image_size(image_path: str | Path) -> tuple[int, int]:
    with Image.open(image_path) as im:
        return int(im.width), int(im.height)


def _offset_from_center(center_px: Sequence[int], image_center_px: Sequence[int]) -> List[int]:
    return [
        int(center_px[0] - image_center_px[0]),
        int(center_px[1] - image_center_px[1]),
    ]


def _axis_actual_from_move(move_result: Dict[str, Any], axis: str, fallback: int) -> int:
    try:
        return int(move_result["after"][axis]["current_pos"])
    except Exception:
        return int(fallback)


def _calc_compensate_target(
    *,
    ctx: Dict[str, Any],
    params: Dict[str, Any],
    image_item: Dict[str, Any],
    clone_item: Dict[str, Any],
) -> Dict[str, Any]:
    plate_cfg = ctx["plate"]
    x_ppm, y_ppm = get_axis_pulses_per_mm(plate_cfg)
    x_sign_for_right, y_sign_for_down = get_view_signs(plate_cfg)

    offset_px = clone_item.get("offset_from_image_center_px", [0, 0])
    mm_per_pixel = image_item["mm_per_pixel"]
    offset_right_mm = float(offset_px[0]) * float(mm_per_pixel["x"])
    offset_down_mm = float(offset_px[1]) * float(mm_per_pixel["y"])

    base_x = image_item.get("stage_x_actual")
    base_y = image_item.get("stage_y_actual")
    if base_x is None:
        base_x = image_item.get("stage_x_target")
    if base_y is None:
        base_y = image_item.get("stage_y_target")
    if base_x is None or base_y is None:
        raise ValueError("无法确定补偿基准坐标：stage_x/stage_y 缺失")

    scale_cfg = params.get("compensate_scale") or {}
    x_scale = float(scale_cfg.get("x", 1.0))
    y_scale = float(scale_cfg.get("y", 1.0))

    # 标准坐标映射：图像水平偏移由 X/电机 1 补偿，图像垂直偏移由 Y/电机 2 补偿。
    target_x = int(round(float(base_x) - x_sign_for_right * offset_right_mm * x_ppm * x_scale))
    target_y = int(round(float(base_y) - y_sign_for_down * offset_down_mm * y_ppm * y_scale))

    return {
        "base_stage": {
            "x": int(base_x),
            "y": int(base_y),
        },
        "offset_from_image_center_px": [int(offset_px[0]), int(offset_px[1])],
        "offset_mm": {
            "view_right_mm": offset_right_mm,
            "view_down_mm": offset_down_mm,
        },
        "scale": {
            "x": x_scale,
            "y": y_scale,
        },
        "compensate_target": {
            "x": target_x,
            "y": target_y,
        },
    }


def _move_to_compensate_target(
    *,
    ctx: Dict[str, Any],
    params: Dict[str, Any],
    target_x: int,
    target_y: int,
) -> Dict[str, Any]:
    raise_if_cancel_requested(params, "before_compensate_move")
    motion = apply_motion_profile_defaults(params.get("motion"))
    plate = ctx["plate"]
    runtime_guard = plate.get("runtime_guard", {}) or {}
    arrival_tolerance = None
    if bool(runtime_guard.get("enabled", False)) and bool(runtime_guard.get("abort_on_motion_failure", True)):
        arrival_tolerance = int(runtime_guard.get("max_err_to_target_pulse", 3000))
    result = move_to_absolute_with_approach(
        port=motion.get("port", DEFAULT_MODBUS_PORT),
        x_target=target_x,
        y_target=target_y,
        profile_vel=int(motion["profile_vel"]),
        profile_acc=int(motion["profile_acc"]),
        profile_dec=int(motion["profile_dec"]),
        x_slave=int(motion.get("x_slave", 1)),
        y_slave=int(motion.get("y_slave", 2)),
        baudrate=int(motion.get("baudrate", 115200)),
        settle_s=float(
            params["settle_s"]
            if params.get("settle_s") is not None
            else motion["settle_s"]
            if motion.get("settle_s") is not None
            else DEFAULT_SCAN_SETTLE_S
        ),
        timeout_s=float(motion.get("timeout_s", 120.0)),
        poll_s=float(motion.get("poll_s", 0.05)),
        arrival_tolerance_pulse=arrival_tolerance,
        stage_limits=plate.get("stage_limits"),
        approach_cfg=params.get("compensate_approach") or {},
    )
    raise_if_cancel_requested(params, "after_compensate_move")
    return result


def _build_image_item_from_capture(
    *,
    image_path: str,
    params: Dict[str, Any],
    stage_x_actual: int,
    stage_y_actual: int,
    detect_entrypoint: str | None,
) -> Dict[str, Any]:
    width, height = _image_size(image_path)
    image_center = [width // 2, height // 2]
    fov_cfg = params["fov_mm"]
    mm_per_pixel = {
        "x": float(fov_cfg["width"]) / float(width),
        "y": float(fov_cfg["height"]) / float(height),
    }

    detect_result = run_detect_on_image(
        image_path,
        entrypoint=detect_entrypoint,
        detect_kwargs={
            **rule_texture_kwargs(detect_entrypoint, params.get("detect_rule_options") or {}),
            "model_dir": params.get("detect_model_dir"),
            "provider": params.get("detect_provider", "cuda"),
            "allow_cpu_fallback": bool(params.get("detect_allow_cpu_fallback", False)),
            "objective_name": params.get("objective_name"),
            "mm_per_pixel": mm_per_pixel,
        },
    )
    clones: List[Dict[str, Any]] = []
    schema_version = int(detect_result.get("schema_version", 1))
    for clone in detect_result.get("clones", []) or []:
        center_px = clone["center_px"]
        offset_px = _offset_from_center(center_px, image_center)
        is_valid = clone.get("is_valid_for_compensation")
        clones.append(
            {
                "clone_id": clone["clone_id"],
                "center_px": center_px,
                "offset_from_image_center_px": offset_px,
                "bbox": clone.get("bbox"),
                "area_px": clone.get("area_px"),
                "score": clone.get("score"),
                "confidence": clone.get("confidence"),
                "segmentation_status": clone.get("segmentation_status"),
                "location_valid": clone.get("location_valid"),
                "eligible_for_10x_centering": clone.get("eligible_for_10x_centering"),
                "is_valid_for_compensation": is_valid,
                "touch_image_border": clone.get("touch_image_border"),
                "image_border_sides": list(clone.get("image_border_sides") or []),
                "image_edge_clipped": clone.get("image_edge_clipped"),
                "is_pickable": (
                    clone.get("is_pickable") is True
                    if schema_version >= 2
                    else clone.get("is_pickable")
                    if clone.get("is_pickable") is not None
                    else is_valid is not False
                ),
                "source_image_path": image_path,
                "stage_x_actual": stage_x_actual,
                "stage_y_actual": stage_y_actual,
            }
        )

    return {
        "index": 1,
        "row_index": 0,
        "col_index": 0,
        "image_path": image_path,
        "stage_x_target": stage_x_actual,
        "stage_y_target": stage_y_actual,
        "stage_x_actual": stage_x_actual,
        "stage_y_actual": stage_y_actual,
        "image_width_px": width,
        "image_height_px": height,
        "image_center_px": image_center,
        "mm_per_pixel": mm_per_pixel,
        "clone_count": int(detect_result.get("clone_count", len(clones))),
        "clones": clones,
    }


def _within_tolerance(offset_px: Sequence[int], cfg: Dict[str, Any]) -> bool:
    tol = cfg.get("tolerance_px", 10)
    if isinstance(tol, dict):
        tol_x = int(tol.get("x", 10))
        tol_y = int(tol.get("y", 10))
    else:
        tol_x = tol_y = int(tol)
    return abs(int(offset_px[0])) <= tol_x and abs(int(offset_px[1])) <= tol_y


def _capture_closed_loop_image(
    *,
    params: Dict[str, Any],
    iteration: int,
) -> Dict[str, Any]:
    cfg = params.get("compensate_closed_loop") or {}
    save_dir = cfg.get("save_dir") or params.get("save_dir")
    if not save_dir:
        raise ValueError("closed_loop 启用时需要 compensate.closed_loop.save_dir 或 capture.save_dir")

    filename_pattern = cfg.get("filename_pattern") or "closed_loop_{task_id}_{well}_iter{iteration:02d}.bmp"
    return capture_single_image(
        save_dir=str(save_dir),
        filename_pattern=str(filename_pattern),
        format_kwargs={
            "task_id": params["task_id"],
            "well": params.get("well_name") or "well",
            "iteration": int(iteration),
        },
        mvs_python_dir=params.get("mvs_python_dir"),
        device_index=int(params["device_index"]),
        serial_number=params.get("serial_number"),
        camera_ip=params.get("camera_ip"),
        pixel_format=params.get("pixel_format", "mono8"),
        exposure_us=params.get("exposure_us"),
        gain=params.get("gain"),
    )


def _run_closed_loop(
    *,
    ctx: Dict[str, Any],
    params: Dict[str, Any],
    first_move_result: Dict[str, Any],
    initial_target: Dict[str, int],
) -> Dict[str, Any]:
    cfg = params.get("compensate_closed_loop") or {}
    max_iterations = int(cfg.get("max_iterations", 2))
    selector_cfg = dict(cfg.get("selector") or {"mode": "nearest_image_center"})
    selector_cfg.setdefault(
        "purpose",
        str((params.get("compensate_selector") or {}).get("purpose") or "pick"),
    )
    detect_entrypoint = cfg.get("detect_entrypoint") or params.get("detect_entrypoint")

    current_x = _axis_actual_from_move(first_move_result, "x", initial_target["x"])
    current_y = _axis_actual_from_move(first_move_result, "y", initial_target["y"])
    iterations: List[Dict[str, Any]] = []

    for iteration in range(1, max_iterations + 1):
        raise_if_cancel_requested(params, f"before_closed_loop_capture:{iteration}")
        report_progress(
            params,
            "compensate",
            min(95, 75 + iteration * 5),
            params.get("well_name"),
            f"closed-loop compensate iteration {iteration}/{max_iterations}",
        )
        capture_result = _capture_closed_loop_image(params=params, iteration=iteration)
        raise_if_cancel_requested(params, f"after_closed_loop_capture:{iteration}")
        image_item = _build_image_item_from_capture(
            image_path=capture_result["saved_path"],
            params=params,
            stage_x_actual=current_x,
            stage_y_actual=current_y,
            detect_entrypoint=detect_entrypoint,
        )
        loop_detect_result = {"images": [image_item]}
        raise_if_cancel_requested(params, f"before_closed_loop_select:{iteration}")
        image_item, clone_item = select_clone_for_compensation(loop_detect_result, selector_cfg)
        calc = _calc_compensate_target(ctx=ctx, params=params, image_item=image_item, clone_item=clone_item)
        in_tolerance = _within_tolerance(calc["offset_from_image_center_px"], cfg)

        item: Dict[str, Any] = {
            "iteration": iteration,
            "capture_result": capture_result,
            "selected_clone": clone_item,
            "offset_from_image_center_px": calc["offset_from_image_center_px"],
            "offset_mm": calc["offset_mm"],
            "in_tolerance": in_tolerance,
        }
        if in_tolerance:
            item["message"] = "target is within tolerance"
            iterations.append(item)
            break

        target = calc["compensate_target"]
        raise_if_cancel_requested(params, f"before_closed_loop_move:{iteration}")
        move_result = _move_to_compensate_target(
            ctx=ctx,
            params=params,
            target_x=int(target["x"]),
            target_y=int(target["y"]),
        )
        current_x = _axis_actual_from_move(move_result, "x", int(target["x"]))
        current_y = _axis_actual_from_move(move_result, "y", int(target["y"]))
        item["compensate_target"] = target
        item["move_result"] = move_result
        iterations.append(item)

    final = iterations[-1] if iterations else {}
    return {
        "enabled": True,
        "max_iterations": max_iterations,
        "tolerance_px": cfg.get("tolerance_px", 10),
        "selector": selector_cfg,
        "iterations": iterations,
        "final_in_tolerance": bool(final.get("in_tolerance", False)),
        "final_offset_from_image_center_px": final.get("offset_from_image_center_px"),
    }


def execute_compensate_on_detect_result(
    ctx: Dict[str, Any],
    params: Dict[str, Any],
    detect_result: Dict[str, Any],
) -> Dict[str, Any]:
    """根据选定克隆相对图像中心的偏差，计算并执行位移台补偿。"""
    selector_cfg = params.get("compensate_selector", {}) or {}
    report_progress(params, "compensate", 5, params.get("well_name"), "selecting clone for compensation")
    raise_if_cancel_requested(params, "before_compensate_select")
    image_item, clone_item = select_clone_for_compensation(detect_result, selector_cfg)
    report_progress(params, "compensate", 25, params.get("well_name"), "clone selected for compensation")

    calc = _calc_compensate_target(ctx=ctx, params=params, image_item=image_item, clone_item=clone_item)
    target = calc["compensate_target"]
    raise_if_cancel_requested(params, "before_initial_compensate_move")
    report_progress(params, "compensate", 45, params.get("well_name"), "moving to compensate target")
    move_result = _move_to_compensate_target(
        ctx=ctx,
        params=params,
        target_x=int(target["x"]),
        target_y=int(target["y"]),
    )
    report_progress(params, "compensate", 70, params.get("well_name"), "compensate target reached")

    closed_loop_result = None
    if bool((params.get("compensate_closed_loop") or {}).get("enabled", False)):
        report_progress(params, "compensate", 75, params.get("well_name"), "closed-loop compensate started")
        closed_loop_result = _run_closed_loop(
            ctx=ctx,
            params=params,
            first_move_result=move_result,
            initial_target={"x": int(target["x"]), "y": int(target["y"])},
        )

    report_progress(params, "compensate", 100, params.get("well_name"), "compensate completed")
    result = {
        "task_id": params["task_id"],
        "status": "success",
        "task_type": "compensate",
        "plate_type": params["plate_type"],
        "well_name": params["well_name"],
        "objective_name": params["objective_name"],
        "selector": selector_cfg,
        "selected_image_index": int(image_item["index"]),
        "selected_clone": clone_item,
        "base_stage": calc["base_stage"],
        "offset_from_image_center_px": calc["offset_from_image_center_px"],
        "offset_mm": calc["offset_mm"],
        "scale": calc["scale"],
        "compensate_target": target,
        "approach": params.get("compensate_approach") or {},
        "move_result": move_result,
        "closed_loop": closed_loop_result,
    }

    output_json = params.get("compensate_output_json")
    if output_json:
        atomic_write_json(output_json, result)

    return result
