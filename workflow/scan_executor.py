"""
执行层脚本,按照扫描计划，真正执行“位移台移动 + 相机拍照 + 结果记录”的过程。

增加：路径点全部生成后，先检查每个点是否在限位范围内。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Any

from workflow.camera_executor import (
    open_camera,
    close_camera,
    capture_with_opened_camera,
)
from workflow.stage_executor import move_to_absolute


def _format_kwargs(params: Dict[str,Any], point: Dict[str,Any]) -> Dict[str,Any]:
    """
    为图片文件名模板生成格式化参数。

    参数
    ----
    params : Dict
        当前任务级参数，通常包含 task_id、well_name 等公共字段。
    point : Dict
        当前扫描点信息，通常来自 scan plan，包含点序号、网格行列、
        视野偏移量以及对应的位移台目标坐标。

    返回
    ----
    Dict
        可直接传给 filename_pattern.format(**kwargs) 的参数字典。

    说明
    ----
    这里单独封装格式化字段，而不是在拍照调用处临时拼接，
    主要是为了：
    1. 统一文件名命名规则；
    2. 避免命名字段分散在主流程中，降低维护成本；
    3. 后续若要调整命名规则，只需改这一处。
    """
    return {
        "task_id": params["task_id"],
        "well": params["well_name"],
        "index": int(point["index"]),
        "row": int(point["row_index"]),
        "col": int(point["col_index"]),
        "vdown": float(point["view_down_mm"]),
        "vright": float(point["view_right_mm"]),
        "x": int(point["stage_x_target"]),
        "y": int(point["stage_y_target"]),
    }

def _get_runtime_guard(plate: Dict[str, Any]) -> Dict[str, Any]:
    cfg = plate.get("runtime_guard", {}) or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "stuck_min_expected_move_pulse": int(cfg.get("stuck_min_expected_move_pulse", 20000)),
        "stuck_max_actual_move_pulse": int(cfg.get("stuck_max_actual_move_pulse", 1000)),
        "max_err_to_target_pulse": int(cfg.get("max_err_to_target_pulse", 3000)),
        "abort_on_motion_failure": bool(cfg.get("abort_on_motion_failure", True)),
    }

def _axis_pos(motion_result: Dict[str, Any], phase: str, axis: str) -> int:
    return int(motion_result[phase][axis]["current_pos"])


def _check_motion_guard(
    plate: Dict[str, Any],
    point: Dict[str, Any],
    motion_result: Dict[str, Any],
) -> None:
    guard = _get_runtime_guard(plate)
    if not guard["enabled"]:
        return

    target_x = int(motion_result["target"]["x"])
    target_y = int(motion_result["target"]["y"])

    before_x = _axis_pos(motion_result, "before", "x")
    before_y = _axis_pos(motion_result, "before", "y")
    after_x = _axis_pos(motion_result, "after", "x")
    after_y = _axis_pos(motion_result, "after", "y")

    err_x = int(motion_result["err_to_target"]["x"])
    err_y = int(motion_result["err_to_target"]["y"])

    expected_move_x = abs(target_x - before_x)
    expected_move_y = abs(target_y - before_y)
    actual_move_x = abs(after_x - before_x)
    actual_move_y = abs(after_y - before_y)

    stuck_min_expected = guard["stuck_min_expected_move_pulse"]
    stuck_max_actual = guard["stuck_max_actual_move_pulse"]
    max_err = guard["max_err_to_target_pulse"]

    stuck_axes = []
    if expected_move_x >= stuck_min_expected and actual_move_x <= stuck_max_actual:
        stuck_axes.append(
            f"x轴疑似卡死(expected={expected_move_x}, actual={actual_move_x}, err={err_x})"
        )
    if expected_move_y >= stuck_min_expected and actual_move_y <= stuck_max_actual:
        stuck_axes.append(
            f"y轴疑似卡死(expected={expected_move_y}, actual={actual_move_y}, err={err_y})"
        )

    err_axes = []
    if abs(err_x) > max_err:
        err_axes.append(f"x轴到位误差过大(err={err_x})")
    if abs(err_y) > max_err:
        err_axes.append(f"y轴到位误差过大(err={err_y})")

    messages = stuck_axes + err_axes
    if messages and guard["abort_on_motion_failure"]:
        raise RuntimeError(
            f"点位 index={point['index']} 运动失败，已中止任务。"
            f" target=({target_x},{target_y}), "
            f"before=({before_x},{before_y}), "
            f"after=({after_x},{after_y}), "
            f"原因: {'; '.join(messages)}"
        )
    
def _write_result(path: str | None, result: Dict[str, Any]) -> None:
    if not path:
        return
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

def execute_scan_capture(ctx: Dict[str,Any], params: Dict[str,Any], plan: Dict[str,Any]) -> Dict[str,Any]:
    """
    按扫描计划执行“位移台移动 + 相机拍照”的采集流程。

    参数
    ----
    ctx : Dict
        运行时上下文。当前函数内部未直接使用，但保留该参数有利于与
        上层 workflow 接口保持一致，也便于后续扩展更多上下文依赖。
    params : Dict
        当前采集任务参数，包含设备索引、曝光、保存目录、运动参数等。
    plan : Dict
        扫描规划结果，至少应包含：
        - points: 扫描点列表
        - reference: 本次扫描参考信息
        - scan_config: 本次扫描配置摘要

    返回
    ----
    Dict
        本次扫描采集的完整结果字典，包括：
        - 任务基本信息
        - 图像数量
        - 每个扫描点的运动结果与拍照结果
        - 可选写盘后的 scan_result.json

    处理流程
    --------
    1. 打开相机，并按任务参数设置曝光/增益；
    2. 遍历扫描点列表：
       - 先移动位移台到目标位置；
       - 再触发相机采集并保存图像；
       - 汇总当前点的运动结果与拍照结果；
    3. 无论过程是否异常，最终都关闭相机；
    4. 汇总任务结果，并按需写入 JSON 文件。

    说明
    ----
    该函数是“扫描执行层”的核心入口。
    它不负责生成扫描点，只负责严格按照 plan 执行。
    换句话说：
    - plan 决定“去哪里拍”
    - execute_scan_capture 决定“怎么逐点执行并记录结果”
    """
    motion = params["motion"]
    captures: List[Dict[str,Any]] = []
    scan_output_json = params.get("scan_output_json")
    
    cam = None
    try:
        # 相机在整个扫描任务期间只打开一次，避免逐点 open/close 带来额外开销，
        # 也更符合实际联机采集的执行方式。
        cam = open_camera(
            device_index=int(params["device_index"]),
            exposure_us=params.get("exposure_us"),
            gain=params.get("gain"),
        )

        for point in plan["points"]:
            # 先执行位移台运动，确保当前视野移动到计划目标点。
            motion_result = move_to_absolute(
                port=motion.get("port", "COM3"),
                x_target=int(point["stage_x_target"]),
                y_target=int(point["stage_y_target"]),
                profile_vel=int(motion["profile_vel"]),
                profile_acc=int(motion["profile_acc"]),
                profile_dec=int(motion["profile_dec"]),
                x_slave=int(motion.get("x_slave", 1)),
                y_slave=int(motion.get("y_slave", 2)),
                baudrate=int(motion.get("baudrate", 115200)),
                settle_s=float(params["settle_s"]),
            )
            _check_motion_guard(ctx["plate"], point, motion_result)
            # 位移稳定后执行拍照，并将当前点信息写入文件名模板字段。
            capture_result = capture_with_opened_camera(
                cam=cam,
                save_dir=params["save_dir"],
                filename_pattern=params["filename_pattern"],
                format_kwargs=_format_kwargs(params, point),
            )

            # 将“规划点信息 + 运动结果 + 采集结果”合并记录，
            # 方便后续追溯每张图对应的空间位置与执行状态。
            captures.append(
                {
                    **point,
                    "motion_result": motion_result,
                    "capture_result": capture_result,
                }
            )

        result = {
            "task_id": params["task_id"],
            "status": "success",
            "task_type": params["task_type"],
            "plate_type": params["plate_type"],
            "well_name": params["well_name"],
            "objective_name": params["objective_name"],
            "reference": plan["reference"],
            "scan_config": plan["scan_config"],
            "stage_limit_precheck": plan.get("stage_limit_precheck"),
            "image_count": len(captures),
            "captures": captures,
        }
        _write_result(scan_output_json, result)
        return result

    except Exception as exc:
        failed_result = {
            "task_id": params["task_id"],
            "status": "failed",
            "task_type": params["task_type"],
            "plate_type": params["plate_type"],
            "well_name": params["well_name"],
            "objective_name": params["objective_name"],
            "reference": plan["reference"],
            "scan_config": plan["scan_config"],
            "stage_limit_precheck": plan.get("stage_limit_precheck"),
            "completed_image_count": len(captures),
            "captures": captures,
            "error": str(exc),
        }
        _write_result(scan_output_json, failed_result)
        raise

    finally:
        close_camera(cam)
            
    # finally:
    #     # 无论中途是否异常，都要确保相机被关闭，避免设备句柄泄漏。
    #     close_camera(cam)

    # # 汇总任务级结果。该结构既可直接返回给上层，也适合序列化落盘。
    # result = {
    #     "task_id": params["task_id"],
    #     "status": "success",
    #     "task_type": params["task_type"],
    #     "plate_type": params["plate_type"],
    #     "well_name": params["well_name"],
    #     "objective_name": params["objective_name"],
    #     "reference": plan["reference"],
    #     "scan_config": plan["scan_config"],
    #     "image_count": len(captures),
    #     "captures": captures,
    # }

    # # 若任务参数中指定了扫描结果 JSON 输出路径，则同步落盘。
    # # 这样后续调度系统、识别流程或人工检查都可以直接读取该结果文件。
    # scan_output_json = params.get("scan_output_json")
    # if scan_output_json:
    #     out_path = Path(scan_output_json)
    #     out_path.parent.mkdir(parents=True, exist_ok=True)
    #     out_path.write_text(
    #         json.dumps(result, ensure_ascii=False, indent=2),
    #         encoding="utf-8",
    #     )

    # return result
