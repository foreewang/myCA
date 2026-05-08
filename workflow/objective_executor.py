from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from devices.motion.modbus import ModbusRTUClient
from devices.motion.MotorManager import MotorManager


class ObjectiveSwitchError(RuntimeError):
    pass


def _split_cfg(objectives_root_cfg: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    if "objectives" not in objectives_root_cfg:
        raise ObjectiveSwitchError("objectives.yaml 顶层缺少 objectives")
    obj_map = objectives_root_cfg.get("objectives") or {}
    state_cfg = objectives_root_cfg.get("state") or {}
    hw_cfg = objectives_root_cfg.get("hardware") or {}
    if not obj_map:
        raise ObjectiveSwitchError("objectives.yaml 中未定义任何物镜")
    return obj_map, state_cfg, hw_cfg


def _load_state(state_file: Path) -> Dict[str, Any]:
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state_file: Path, payload: Dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _move_axis(motor: MotorManager, target: int, vel: int, acc: int, dec: int) -> Dict[str, Any]:
    err = motor.pp_absolute_move(
        target_pos=int(target),
        profile_vel=int(vel),
        profile_acc=int(acc),
        profile_dec=int(dec),
    )
    if err is None:
        raise ObjectiveSwitchError(f"电机移动失败，target={target}")
    return {
        "target": int(target),
        "err_to_target_pulse": int(err),
    }


def ensure_objective_for_task(
    task_cfg: Dict[str, Any],
    objectives_root_cfg: Dict[str, Any],
    extra_context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    基于 MotorManager 接入真实物镜切换：
    - 物镜轴: slave=4
    - 调焦轴: slave=3

    task_cfg 里只需要提供:
      objective: "4x" / "10x"
    """
    requested = task_cfg.get("objective")
    if not requested:
        return {
            "requested_objective": None,
            "previous_objective": None,
            "current_objective": None,
            "switched": False,
            "message": "任务未声明 objective，跳过物镜切换",
        }

    obj_map, state_cfg, hw_cfg = _split_cfg(objectives_root_cfg)
    if requested not in obj_map:
        raise ObjectiveSwitchError(f"objectives.yaml 中不存在物镜: {requested}")

    objective_cfg = obj_map[requested] or {}
    switch_cfg = objective_cfg.get("switch") or {}
    if not switch_cfg.get("enabled", True):
        return {
            "requested_objective": requested,
            "previous_objective": None,
            "current_objective": None,
            "switched": False,
            "message": f"{requested} 未启用 switch.enabled，跳过物镜切换",
        }

    mode = str(switch_cfg.get("mode") or "motor_manager").lower()
    if mode != "motor_manager":
        raise ObjectiveSwitchError(f"当前 objective_executor 只支持 mode=motor_manager，收到: {mode!r}")

    state_enabled = bool(state_cfg.get("enabled", True))
    state_file = Path(state_cfg.get("state_file") or "C:/colony_system/data/objective_state.json")
    previous = None
    if state_enabled:
        state = _load_state(state_file)
        previous = state.get("current_objective")
        if previous is None:
            previous = state_cfg.get("assume_initial")

    if previous == requested:
        return {
            "requested_objective": requested,
            "previous_objective": previous,
            "current_objective": requested,
            "switched": False,
            "state_file": str(state_file) if state_enabled else None,
            "message": "当前物镜已是目标倍率，无需切换",
        }

    modbus_cfg = hw_cfg.get("modbus") or {}
    port = modbus_cfg.get("port")
    baudrate = modbus_cfg.get("baudrate", 115200)
    if not port:
        raise ObjectiveSwitchError("objectives.yaml.hardware.modbus.port 未配置")

    objective_slave = int((hw_cfg.get("objective_axis") or {}).get("slave", 4))
    focus_slave = int((hw_cfg.get("focus_axis") or {}).get("slave", 3))

    objective_target = switch_cfg.get("objective_target_pos")
    focus_target = switch_cfg.get("focus_target_pos")
    if objective_target is None:
        raise ObjectiveSwitchError(f"{requested} 缺少 switch.objective_target_pos")
    if focus_target is None:
        raise ObjectiveSwitchError(f"{requested} 缺少 switch.focus_target_pos")

    objective_move = {}
    focus_move = {}

    with ModbusRTUClient(port=port, baudrate=int(baudrate)) as client:
        objective_motor = MotorManager(client, slave=objective_slave)
        focus_motor = MotorManager(client, slave=focus_slave)

        objective_move = _move_axis(
            motor=objective_motor,
            target=int(objective_target),
            vel=int(switch_cfg.get("objective_profile_vel", 100000)),
            acc=int(switch_cfg.get("objective_profile_acc", 100000)),
            dec=int(switch_cfg.get("objective_profile_dec", 100000)),
        )

        focus_move = _move_axis(
            motor=focus_motor,
            target=int(focus_target),
            vel=int(switch_cfg.get("focus_profile_vel", 100000)),
            acc=int(switch_cfg.get("focus_profile_acc", 100000)),
            dec=int(switch_cfg.get("focus_profile_dec", 100000)),
        )

    result = {
        "requested_objective": requested,
        "previous_objective": previous,
        "current_objective": requested,
        "switched": True,
        "mode": "motor_manager",
        "state_file": str(state_file) if state_enabled else None,
        "objective_axis_slave": objective_slave,
        "focus_axis_slave": focus_slave,
        "objective_move_result": objective_move,
        "focus_move_result": focus_move,
        "message": "物镜与焦距切换完成",
    }

    if state_enabled:
        _save_state(
            state_file,
            {
                "current_objective": requested,
                "previous_objective": previous,
                "last_result": result,
            },
        )

    return result


def attach_objective_result(result: Dict[str, Any], objective_result: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return result
    out = dict(result)
    out["objective_result"] = objective_result
    return out