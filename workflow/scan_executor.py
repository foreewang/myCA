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


def _should_run_autofocus_at_this_point(
    params: Dict[str, Any],
    point: Dict[str, Any],
) -> tuple[bool, str]:
    """判断是否应在当前扫描点执行 autofocus。

    当前工程策略：默认在每个孔第一个扫描点完成移动并稳定后、第一张拍照前执行一次。
    可通过 config/autofocus.yaml 的 trigger.scope 调整：
    - once_per_well: 每个孔第一个扫描点执行一次，默认值；
    - once_per_task: 整个任务只在第一个孔第一个扫描点执行一次；
    - disabled: 不在扫描点执行。
    """
    decision = params.get("autofocus_decision") or {}
    if not bool(decision.get("should_run", False)):
        return False, "autofocus_decision_false"

    try:
        point_index = int(point.get("index", 0))
    except Exception:
        point_index = 0

    if point_index != 1:
        return False, "not_first_scan_point"

    autofocus_cfg = params.get("autofocus_cfg") or {}
    trigger_cfg = autofocus_cfg.get("trigger") or {}
    scope = str(trigger_cfg.get("scope") or "once_per_well").strip().lower()

    if scope in {"disabled", "disable", "none", "off", "false"}:
        return False, "autofocus_scope_disabled"

    if scope == "once_per_task":
        runtime_state = params.setdefault("_autofocus_runtime_state", {})
        if runtime_state.get("task_done", False):
            return False, "autofocus_once_per_task_already_done"
        runtime_state["task_done"] = True
        return True, "once_per_task"

    # 默认每个孔第一个扫描点执行一次。
    return True, scope or "once_per_well"


def _execute_autofocus_before_capture(
    ctx: Dict[str, Any],
    params: Dict[str, Any],
    point: Dict[str, Any],
    scope_reason: str,
) -> Dict[str, Any]:
    from workflow.autofocus_executor import execute_autofocus_for_task

    autofocus_result = execute_autofocus_for_task(
        ctx=ctx,
        task_cfg=ctx["task"],
        objective_result=params.get("objective_result", {}) or {},
        autofocus_cfg=params.get("autofocus_cfg", {}) or {},
    )
    autofocus_result["run_at"] = "before_first_capture_after_stage_move"
    autofocus_result["scope_reason"] = scope_reason
    autofocus_result["well_name"] = params.get("well_name")
    autofocus_result["scan_point_index"] = int(point.get("index", 0))
    autofocus_result["stage_x_target"] = int(point.get("stage_x_target", 0))
    autofocus_result["stage_y_target"] = int(point.get("stage_y_target", 0))
    autofocus_result.setdefault(
        "trigger_reason",
        (params.get("autofocus_decision") or {}).get("reason"),
    )
    return autofocus_result

def execute_scan_capture(ctx: Dict[str,Any], params: Dict[str,Any], plan: Dict[str,Any], cam = None) -> Dict[str,Any]:
    """
    按扫描计划执行“位移台移动 + 可选 autofocus + 相机拍照”的采集流程。

    autofocus 的工程时机：
    - run_task 只负责物镜切换和生成 autofocus_decision；
    - 本函数在第一个扫描点完成 XY 移动并等待稳定后、第一张拍照前执行 autofocus；
    - 这样 autofocus 看到的是目标孔实际观察视野，而不是上一次任务末尾位置。
    """
    motion = params["motion"]
    captures: List[Dict[str,Any]] = []
    scan_output_json = params.get("scan_output_json")
    before_first_capture_autofocus_result = None

    owned_cam = False
    local_cam = cam

    try:
        for point in plan["points"]:
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

            point_autofocus_result = None
            should_autofocus, autofocus_scope_reason = _should_run_autofocus_at_this_point(params, point)
            if should_autofocus:
                # autofocus 会自行打开/关闭第三方配置中的相机。
                # 因此本函数采用懒加载策略：先 autofocus，再打开正式采集相机，避免 MVS 设备句柄冲突。
                if local_cam is not None and not owned_cam:
                    raise RuntimeError(
                        "当前扫描点需要 autofocus，但外部已传入打开的相机对象。"
                        "为避免 MVS 相机句柄冲突，请在需要 autofocus 时不要提前打开 shared_cam。"
                    )

                point_autofocus_result = _execute_autofocus_before_capture(
                    ctx=ctx,
                    params=params,
                    point=point,
                    scope_reason=autofocus_scope_reason,
                )
                if before_first_capture_autofocus_result is None:
                    before_first_capture_autofocus_result = point_autofocus_result

            if local_cam is None:
                local_cam = open_camera(
                    mvs_python_dir=params.get("mvs_python_dir"),
                    device_index=int(params["device_index"]),
                    serial_number=params.get("serial_number"),
                    exposure_us=params.get("exposure_us"),
                    gain=params.get("gain"),
                )
                owned_cam = True

            capture_result = capture_with_opened_camera(
                cam=local_cam,
                save_dir=params["save_dir"],
                filename_pattern=params["filename_pattern"],
                format_kwargs=_format_kwargs(params, point),
            )

            capture_item = {
                **point,
                "motion_result": motion_result,
                "capture_result": capture_result,
            }
            if point_autofocus_result is not None:
                capture_item["autofocus_result"] = point_autofocus_result

            captures.append(capture_item)

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
            "autofocus_decision": params.get("autofocus_decision"),
            "before_first_capture_autofocus_result": before_first_capture_autofocus_result,
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
            "autofocus_decision": params.get("autofocus_decision"),
            "before_first_capture_autofocus_result": before_first_capture_autofocus_result,
            "completed_image_count": len(captures),
            "captures": captures,
            "error": str(exc),
        }
        _write_result(scan_output_json, failed_result)
        raise

    finally:
        if owned_cam and local_cam is not None:
            close_camera(local_cam)
