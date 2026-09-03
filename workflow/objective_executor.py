"""根据任务物镜要求切换物镜位并记录物镜状态。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict

from devices.motion.modbus import (
    ModbusRTUClient,
    MotionCanceled,
    MotionStopFailed,
)
from devices.motion.MotorManager import MotorManager
from workflow.file_io import atomic_write_json, read_json_with_retry
from workflow.hardware_guard import latch_hardware_unsafe
from workflow.task_control import CancellationSignal, TaskCanceled
from workflow.task_store import task_objective_name


PROJECT_ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)


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
        return read_json_with_retry(state_file)
    except Exception:
        return {}


def _save_state(state_file: Path, payload: Dict[str, Any]) -> None:
    atomic_write_json(state_file, payload)


def _move_axis(
    motor: MotorManager,
    target: int,
    vel: int,
    acc: int,
    dec: int,
    cancel_check: Callable[[], bool] | None = None,
) -> Dict[str, Any]:
    signal = CancellationSignal(cancel_check) if cancel_check is not None else None
    if signal is not None and signal.is_set():
        raise TaskCanceled("task canceled before objective/focus motion")
    try:
        err = motor.pp_absolute_move(
            target_pos=int(target),
            profile_vel=int(vel),
            profile_acc=int(acc),
            profile_dec=int(dec),
            stop_event=signal,
        )
    except MotionCanceled as exc:
        raise TaskCanceled("task canceled during objective/focus motion") from exc
    except MotionStopFailed as exc:
        message = f"电机急停失败，硬件状态未知，禁止继续运行；target={int(target)}"
        latch_hardware_unsafe("OBJECTIVE_MOTION_STOP_FAILED", message)
        raise ObjectiveSwitchError(message) from exc
    if err is None:
        raise ObjectiveSwitchError(f"电机移动失败，target={target}")
    current = motor.get_current_position()
    if isinstance(current, bool) or not isinstance(current, int):
        raise ObjectiveSwitchError(f"电机到位后位置读回失败，target={int(target)}")
    if abs(int(current) - int(target)) > 50:
        raise ObjectiveSwitchError(
            f"电机到位读回误差过大，target={int(target)}, current={int(current)}, tolerance=50"
        )
    return {
        "target": int(target),
        "err_to_target_pulse": int(err),
        "current_pos": int(current),
    }


def ensure_objective_for_task(
    task_cfg: Dict[str, Any],
    objectives_root_cfg: Dict[str, Any],
    extra_context: Dict[str, Any] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Dict[str, Any]:
    """
    基于 MotorManager 接入真实物镜切换：
    - 物镜轴: slave=4
    - 调焦轴: slave=3

    task_cfg 里只需要提供:
      objective_name: "4x" / "10x"
    兼容旧字段 objective，但内部统一使用 objective_name。
    """
    requested = task_objective_name(task_cfg)
    if not requested:
        return {
            "requested_objective": None,
            "previous_objective": None,
            "current_objective": None,
            "switched": False,
            "message": "任务未声明 objective_name，跳过物镜切换",
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
    state_file = Path(state_cfg.get("state_file") or PROJECT_ROOT / "data" / "objective_state.json")
    if not state_file.is_absolute():
        state_file = PROJECT_ROOT / state_file
    state: Dict[str, Any] = {}
    previous = None
    state_ready = False
    if state_enabled:
        state = _load_state(state_file)
        previous = state.get("current_objective")
        if previous is None:
            previous = state.get("last_confirmed_objective")
        if previous is None:
            previous = state_cfg.get("assume_initial")
        state_ready = bool(
            state.get("switch_status") == "ready"
            and state.get("current_objective") == requested
        )

    modbus_cfg = hw_cfg.get("modbus") or {}
    port = modbus_cfg.get("port")
    baudrate = modbus_cfg.get("baudrate", 115200)
    if not port:
        raise ObjectiveSwitchError("objectives.yaml.hardware.modbus.port 未配置")

    objective_slave = int((hw_cfg.get("objective_axis") or {}).get("slave", 4))
    focus_axis_cfg = hw_cfg.get("focus_axis") or {}
    focus_slave = int(focus_axis_cfg.get("slave", 3))
    objective_switch_collision_limit = focus_axis_cfg.get("objective_switch_collision_limit_pos")

    objective_target = switch_cfg.get("objective_target_pos")
    focus_target = switch_cfg.get("focus_target_pos")
    focus_collision_limit = switch_cfg.get("focus_collision_limit_pos")
    if objective_target is None:
        raise ObjectiveSwitchError(f"{requested} 缺少 switch.objective_target_pos")
    if focus_target is None:
        raise ObjectiveSwitchError(f"{requested} 缺少 switch.focus_target_pos")
    if isinstance(focus_collision_limit, bool) or not isinstance(focus_collision_limit, int):
        raise ObjectiveSwitchError(f"{requested} 缺少有效的 switch.focus_collision_limit_pos")
    if int(focus_target) < focus_collision_limit:
        raise ObjectiveSwitchError(
            f"{requested} 的焦点位置 {focus_target} 小于碰撞安全限位 {focus_collision_limit}"
        )
    if isinstance(objective_switch_collision_limit, bool) or not isinstance(
        objective_switch_collision_limit, int
    ):
        raise ObjectiveSwitchError("缺少有效的 hardware.focus_axis.objective_switch_collision_limit_pos")
    if int(focus_target) < objective_switch_collision_limit:
        raise ObjectiveSwitchError(
            f"{requested} 的焦点位置 {focus_target} 小于物镜切换碰撞安全限位 "
            f"{objective_switch_collision_limit}"
        )

    objective_move = {}
    focus_move = {}
    focus_position_before_switch = None

    if cancel_check is not None and bool(cancel_check()):
        raise TaskCanceled("task canceled before objective switch")

    with ModbusRTUClient(port=port, baudrate=int(baudrate)) as client:
        objective_motor = MotorManager(client, slave=objective_slave)
        focus_motor = MotorManager(client, slave=focus_slave)

        try:
            focus_position_before_switch = focus_motor.get_current_position()
        except Exception as exc:
            raise ObjectiveSwitchError("切换物镜前读取电机3当前位置失败") from exc

        if focus_position_before_switch is None:
            raise ObjectiveSwitchError("切换物镜前读取电机3当前位置失败：未返回有效位置")
        if isinstance(focus_position_before_switch, bool) or not isinstance(
            focus_position_before_switch, int
        ):
            raise ObjectiveSwitchError(
                "切换物镜前读取电机3当前位置失败：返回值不是整数脉冲位置"
            )
        if focus_position_before_switch < objective_switch_collision_limit:
            raise ObjectiveSwitchError(
                f"切换物镜前电机3当前位置 {focus_position_before_switch} 小于物镜切换碰撞安全限位 "
                f"{objective_switch_collision_limit}"
            )

        if state_ready:
            try:
                objective_position = objective_motor.get_current_position()
            except Exception as exc:
                raise ObjectiveSwitchError("跳过物镜切换前读取物镜轴当前位置失败") from exc
            if isinstance(objective_position, bool) or not isinstance(objective_position, int):
                raise ObjectiveSwitchError("跳过物镜切换前读取物镜轴当前位置失败：未返回整数脉冲位置")
            objective_matches = abs(int(objective_position) - int(objective_target)) <= 50
            focus_matches = abs(int(focus_position_before_switch) - int(focus_target)) <= 50
            if objective_matches and focus_matches:
                if cancel_check is not None and bool(cancel_check()):
                    raise TaskCanceled("task canceled before verified objective reuse")
                return {
                    "requested_objective": requested,
                    "previous_objective": previous,
                    "current_objective": requested,
                    "switched": False,
                    "state_file": str(state_file),
                    "objective_axis_position": int(objective_position),
                    "focus_axis_position": int(focus_position_before_switch),
                    "hardware_verified": True,
                    "message": "Current objective and focus positions verified; no change needed",
                }

        # Persist an indeterminate state before the first physical command.
        # A crash, timeout, or cancellation after only one axis moved must not
        # leave the old objective name looking trustworthy on the next task.
        if state_enabled:
            _save_state(
                state_file,
                {
                    "current_objective": None,
                    "previous_objective": previous,
                    "last_confirmed_objective": previous,
                    "requested_objective": requested,
                    "switch_status": "switching",
                    "phase": "objective",
                },
            )

        try:
            objective_move = _move_axis(
                motor=objective_motor,
                target=int(objective_target),
                vel=int(switch_cfg.get("objective_profile_vel", 100000)),
                acc=int(switch_cfg.get("objective_profile_acc", 100000)),
                dec=int(switch_cfg.get("objective_profile_dec", 100000)),
                cancel_check=cancel_check,
            )

            if state_enabled:
                _save_state(
                    state_file,
                    {
                        "current_objective": None,
                        "previous_objective": previous,
                        "last_confirmed_objective": previous,
                        "requested_objective": requested,
                        "switch_status": "switching",
                        "phase": "focus",
                        "objective_axis_position": objective_move["current_pos"],
                    },
                )

            objective_signal = CancellationSignal(cancel_check) if cancel_check is not None else None
            if objective_signal is not None and objective_signal.is_set():
                raise TaskCanceled("task canceled between objective and focus motion")

            focus_move = _move_axis(
                motor=focus_motor,
                target=int(focus_target),
                vel=int(switch_cfg.get("focus_profile_vel", 100000)),
                acc=int(switch_cfg.get("focus_profile_acc", 100000)),
                dec=int(switch_cfg.get("focus_profile_dec", 100000)),
                cancel_check=cancel_check,
            )
        except BaseException as exc:
            if state_enabled:
                try:
                    _save_state(
                        state_file,
                        {
                            "current_objective": None,
                            "previous_objective": previous,
                            "last_confirmed_objective": previous,
                            "requested_objective": requested,
                            "switch_status": "unknown",
                            "phase": "failed",
                            "error_type": type(exc).__name__,
                        },
                    )
                except Exception:
                    logger.exception("failed to persist unknown objective state after motion failure")
            raise

    result = {
        "requested_objective": requested,
        "previous_objective": previous,
        "current_objective": requested,
        "switched": True,
        "mode": "motor_manager",
        "state_file": str(state_file) if state_enabled else None,
        "objective_axis_slave": objective_slave,
        "focus_axis_slave": focus_slave,
        "focus_position_before_switch": focus_position_before_switch,
        "objective_move_result": objective_move,
        "focus_move_result": focus_move,
        "message": "Objective and focus switching completed",
    }

    if state_enabled:
        _save_state(
            state_file,
            {
                "current_objective": requested,
                "previous_objective": previous,
                "last_confirmed_objective": requested,
                "switch_status": "ready",
                "phase": "complete",
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
