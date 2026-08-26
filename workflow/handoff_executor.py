"""根据交接配置驱动位移台移动到指定交接点并校验到位误差。"""
from __future__ import annotations

from typing import Any, Dict

from workflow.stage_executor import StageMotionError, move_to_absolute


class HandoffError(RuntimeError):
    pass


def _require(mapping: Dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise HandoffError(f"缺少必填字段 {key!r}，位置: {where}")
    return mapping[key]


def _get_root_cfg(handoff_root_cfg: Dict[str, Any]) -> Dict[str, Any]:
    if "handoff" in handoff_root_cfg:
        cfg = handoff_root_cfg.get("handoff") or {}
    else:
        cfg = handoff_root_cfg or {}
    if not isinstance(cfg, dict) or not cfg:
        raise HandoffError("handoff.yaml 配置为空")
    return cfg


def _resolve_action_cfg(root_cfg: Dict[str, Any], action: str) -> Dict[str, Any]:
    actions = root_cfg.get("actions") or {}
    if action not in actions:
        raise HandoffError(f"handoff.actions 中未定义动作: {action!r}")
    cfg = actions[action] or {}
    point_name = _require(cfg, "point", f"handoff.actions.{action}")
    ready_state = _require(cfg, "ready_state", f"handoff.actions.{action}")
    return {
        "action": action,
        "point_name": point_name,
        "ready_state": ready_state,
        "message": cfg.get("message"),
    }


def _resolve_point_cfg(root_cfg: Dict[str, Any], plate_type: str, point_name: str) -> Dict[str, Any]:
    points = root_cfg.get("points") or {}
    if point_name not in points:
        raise HandoffError(f"handoff.points 中未定义点位: {point_name!r}")
    point_cfg = dict(points[point_name] or {})

    plate_overrides = root_cfg.get("plate_overrides") or {}
    if plate_type in plate_overrides:
        override_map = plate_overrides.get(plate_type) or {}
        if point_name in override_map:
            point_cfg.update(override_map.get(point_name) or {})

    for key in ("x", "y"):
        if point_cfg.get(key) is None:
            raise HandoffError(f"点位 {point_name!r} 缺少 {key}")
    return point_cfg


def _resolve_motion_cfg(task_cfg: Dict[str, Any], root_cfg: Dict[str, Any], point_cfg: Dict[str, Any]) -> Dict[str, Any]:
    task_motion = task_cfg.get("motion", {}) or {}
    hardware_cfg = root_cfg.get("hardware", {}) or {}
    modbus_cfg = hardware_cfg.get("modbus", {}) or {}
    x_axis_cfg = hardware_cfg.get("x_axis", {}) or {}
    y_axis_cfg = hardware_cfg.get("y_axis", {}) or {}

    port = task_motion.get("port") or modbus_cfg.get("port")
    if not port:
        raise HandoffError("未配置 Modbus 串口 port，可在 task.motion.port 或 handoff.hardware.modbus.port 中提供")

    arrival_tolerance = int(
        task_motion.get(
            "arrival_tolerance_pulse",
            point_cfg.get("arrival_tolerance_pulse", 3000),
        )
    )
    if arrival_tolerance < 0:
        raise HandoffError("arrival_tolerance_pulse 不能为负数")

    return {
        "port": str(port),
        "baudrate": int(task_motion.get("baudrate", modbus_cfg.get("baudrate", 115200))),
        "x_slave": int(task_motion.get("x_slave", x_axis_cfg.get("slave", 1))),
        "y_slave": int(task_motion.get("y_slave", y_axis_cfg.get("slave", 2))),
        "profile_vel": int(task_motion.get("profile_vel", point_cfg.get("profile_vel", 500000))),
        "profile_acc": int(task_motion.get("profile_acc", point_cfg.get("profile_acc", 100000))),
        "profile_dec": int(task_motion.get("profile_dec", point_cfg.get("profile_dec", 100000))),
        "timeout_s": float(task_motion.get("timeout_s", point_cfg.get("timeout_s", 120.0))),
        "poll_s": float(task_motion.get("poll_s", point_cfg.get("poll_s", 0.05))),
        "settle_s": float(task_motion.get("settle_s", point_cfg.get("settle_s", 0.5))),
        "arrival_tolerance_pulse": arrival_tolerance,
    }


def _check_arrival_tolerance(axis_name: str, err: int, tolerance: int, target: int) -> None:
    if abs(int(err)) > int(tolerance):
        raise HandoffError(
            f"{axis_name}轴到位误差超过阈值: "
            f"target={int(target)}, err={int(err)}, tolerance={int(tolerance)}"
        )


def execute_handoff_task(
    task_cfg: Dict[str, Any],
    handoff_root_cfg: Dict[str, Any],
    plate_cfg: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    root_cfg = _get_root_cfg(handoff_root_cfg)

    task_id = str(task_cfg.get("task_id") or "")
    plate_type = str(task_cfg.get("plate_type") or "")
    if not plate_type:
        raise HandoffError("handoff 任务要求提供 plate_type")

    handoff_task_cfg = task_cfg.get("handoff", {}) or {}
    action = str(handoff_task_cfg.get("action") or "").strip().lower()
    if action not in {"load_in", "unload_out"}:
        raise HandoffError("handoff.action 只支持 load_in / unload_out")

    action_cfg = _resolve_action_cfg(root_cfg, action)
    point_cfg = _resolve_point_cfg(root_cfg, plate_type, action_cfg["point_name"])
    motion_cfg = _resolve_motion_cfg(task_cfg, root_cfg, point_cfg)

    target_x = int(point_cfg["x"])
    target_y = int(point_cfg["y"])
    try:
        move_result = move_to_absolute(
            port=motion_cfg["port"],
            x_target=target_x,
            y_target=target_y,
            profile_vel=motion_cfg["profile_vel"],
            profile_acc=motion_cfg["profile_acc"],
            profile_dec=motion_cfg["profile_dec"],
            x_slave=motion_cfg["x_slave"],
            y_slave=motion_cfg["y_slave"],
            baudrate=motion_cfg["baudrate"],
            settle_s=motion_cfg["settle_s"],
            timeout_s=motion_cfg["timeout_s"],
            poll_s=motion_cfg["poll_s"],
            arrival_tolerance_pulse=motion_cfg["arrival_tolerance_pulse"],
            stage_limits=(plate_cfg or {}).get("stage_limits"),
        )
    except StageMotionError as exc:
        raise HandoffError(str(exc)) from exc

    return {
        "task_id": task_id,
        "status": "success",
        "task_type": "handoff",
        "action": action,
        "plate_type": plate_type,
        "handoff_point": {
            "name": action_cfg["point_name"],
            "x": target_x,
            "y": target_y,
            "meaning": point_cfg.get("meaning"),
        },
        "ready_state": action_cfg["ready_state"],
        "message": action_cfg.get("message") or "位移台已到机械臂对接点",
        "motion": motion_cfg,
        "move_result": {
            **move_result,
            "x_err_to_target_pulse": int(move_result["err_to_target"]["x"]),
            "y_err_to_target_pulse": int(move_result["err_to_target"]["y"]),
        },
    }
