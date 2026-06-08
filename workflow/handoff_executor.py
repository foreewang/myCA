from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

# 如果你项目里的导入路径不是这两个，请改成你本地实际路径
from devices.motion.modbus import ModbusRTUClient
from devices.motion.MotorManager import MotorManager


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

    for k in ("x", "y"):
        if point_cfg.get(k) is None:
            raise HandoffError(f"点位 {point_name!r} 缺少 {k}")
    return point_cfg


def _resolve_motion_cfg(task_cfg: Dict[str, Any], root_cfg: Dict[str, Any], point_cfg: Dict[str, Any]) -> Dict[str, Any]:
    task_motion = task_cfg.get("motion", {}) or {}
    hw = root_cfg.get("hardware", {}) or {}
    modbus_cfg = hw.get("modbus", {}) or {}
    x_axis_cfg = hw.get("x_axis", {}) or {}
    y_axis_cfg = hw.get("y_axis", {}) or {}

    port = task_motion.get("port") or modbus_cfg.get("port")
    if not port:
        raise HandoffError("未配置 Modbus 串口 port，可在 task.motion.port 或 handoff.hardware.modbus.port 中提供")

    return {
        "port": port,
        "baudrate": int(task_motion.get("baudrate", modbus_cfg.get("baudrate", 115200))),
        "x_slave": int(task_motion.get("x_slave", x_axis_cfg.get("slave", 1))),
        "y_slave": int(task_motion.get("y_slave", y_axis_cfg.get("slave", 2))),
        "profile_vel": int(task_motion.get("profile_vel", point_cfg.get("profile_vel", 500000))),
        "profile_acc": int(task_motion.get("profile_acc", point_cfg.get("profile_acc", 100000))),
        "profile_dec": int(task_motion.get("profile_dec", point_cfg.get("profile_dec", 100000))),
        "timeout_s": float(task_motion.get("timeout_s", point_cfg.get("timeout_s", 120.0))),
        "settle_s": float(task_motion.get("settle_s", point_cfg.get("settle_s", 0.5))),
    }


def execute_handoff_task(task_cfg: Dict[str, Any], handoff_root_cfg: Dict[str, Any]) -> Dict[str, Any]:
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

    with ModbusRTUClient(port=motion_cfg["port"], baudrate=motion_cfg["baudrate"]) as client:
        x_motor = MotorManager(client, slave=motion_cfg["x_slave"])
        y_motor = MotorManager(client, slave=motion_cfg["y_slave"])

        x_err = x_motor.pp_absolute_move(
            target_pos=target_x,
            profile_vel=motion_cfg["profile_vel"],
            profile_acc=motion_cfg["profile_acc"],
            profile_dec=motion_cfg["profile_dec"],
            timeout=motion_cfg["timeout_s"],
        )
        if x_err is None:
            raise HandoffError(f"X 轴未能到达 handoff 点位: {target_x}")

        y_err = y_motor.pp_absolute_move(
            target_pos=target_y,
            profile_vel=motion_cfg["profile_vel"],
            profile_acc=motion_cfg["profile_acc"],
            profile_dec=motion_cfg["profile_dec"],
            timeout=motion_cfg["timeout_s"],
        )
        if y_err is None:
            raise HandoffError(f"Y 轴未能到达 handoff 点位: {target_y}")

    time.sleep(max(0.0, motion_cfg["settle_s"]))

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
            "target": {"x": target_x, "y": target_y},
            "x_err_to_target_pulse": int(x_err),
            "y_err_to_target_pulse": int(y_err),
        },
    }
