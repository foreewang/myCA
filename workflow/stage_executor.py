"""封装 XY 位移台绝对运动、接近运动、限位检查和到位校验。"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Mapping

from devices.motion.MotorManager import MotorManager
from devices.motion.modbus import ModbusRTUClient

logger = logging.getLogger(__name__)


class StageMotionError(RuntimeError):
    """Raised when an XY stage move cannot be completed safely."""


def _positive_int(value: Any, name: str) -> int:
    try:
        number = int(value)
    except Exception as exc:
        raise StageMotionError(f"{name} must be an integer") from exc
    if number <= 0:
        raise StageMotionError(f"{name} must be positive")
    return number


def _non_negative_float(value: Any, name: str) -> float:
    try:
        number = float(value)
    except Exception as exc:
        raise StageMotionError(f"{name} must be a number") from exc
    if number < 0:
        raise StageMotionError(f"{name} must be non-negative")
    return number


def _positive_float(value: Any, name: str) -> float:
    number = _non_negative_float(value, name)
    if number <= 0:
        raise StageMotionError(f"{name} must be positive")
    return number


def _normalize_stage_limits(stage_limits: Mapping[str, Any] | None) -> Dict[str, Any]:
    cfg = dict(stage_limits or {})
    enabled = bool(cfg.get("enabled", False))
    if not enabled:
        return {"enabled": False}

    required = ("x_min", "x_max", "y_min", "y_max")
    missing = [key for key in required if cfg.get(key) is None]
    if missing:
        raise StageMotionError(f"stage_limits missing required field(s): {', '.join(missing)}")

    limits = {
        "enabled": True,
        "x_min": int(cfg["x_min"]),
        "x_max": int(cfg["x_max"]),
        "y_min": int(cfg["y_min"]),
        "y_max": int(cfg["y_max"]),
        "safety_margin": int(cfg.get("safety_margin", 0)),
    }
    if limits["x_min"] >= limits["x_max"] or limits["y_min"] >= limits["y_max"]:
        raise StageMotionError("stage_limits min must be smaller than max")
    if limits["safety_margin"] < 0:
        raise StageMotionError("stage_limits safety_margin must be non-negative")
    return limits


def _safe_bounds(limits: Mapping[str, Any], axis: str) -> tuple[int, int]:
    margin = int(limits.get("safety_margin", 0))
    return int(limits[f"{axis}_min"]) + margin, int(limits[f"{axis}_max"]) - margin


def _check_target_in_stage_limits(
    *,
    x_target: int,
    y_target: int,
    stage_limits: Mapping[str, Any] | None,
    label: str,
) -> None:
    limits = _normalize_stage_limits(stage_limits)
    if not limits["enabled"]:
        return

    x_lo, x_hi = _safe_bounds(limits, "x")
    y_lo, y_hi = _safe_bounds(limits, "y")
    if x_lo > x_hi or y_lo > y_hi:
        raise StageMotionError("stage_limits safety_margin leaves no valid travel range")
    if not (x_lo <= int(x_target) <= x_hi):
        raise StageMotionError(f"{label}.x={int(x_target)} is outside safe range [{x_lo}, {x_hi}]")
    if not (y_lo <= int(y_target) <= y_hi):
        raise StageMotionError(f"{label}.y={int(y_target)} is outside safe range [{y_lo}, {y_hi}]")


def _require_axis_position(snapshot: Mapping[str, Any], axis_name: str) -> int:
    pos = snapshot.get("current_pos")
    if pos is None:
        raise StageMotionError(f"failed to read {axis_name} current position")
    return int(pos)


def _check_arrival_tolerance(
    *,
    err_to_target: Mapping[str, int],
    arrival_tolerance_pulse: int | None,
) -> None:
    if arrival_tolerance_pulse is None:
        return
    tolerance = int(arrival_tolerance_pulse)
    if tolerance < 0:
        raise StageMotionError("arrival_tolerance_pulse must be non-negative")
    for axis in ("x", "y"):
        err = int(err_to_target[axis])
        if abs(err) > tolerance:
            raise StageMotionError(f"{axis} arrival error is too large: err={err}, tolerance={tolerance}")


def _quick_stop_xy(x_motor: MotorManager | None, y_motor: MotorManager | None) -> None:
    for axis_name, motor in (("x", x_motor), ("y", y_motor)):
        if motor is None:
            continue
        try:
            if not motor.client.quick_stop(motor.slave):
                logger.warning("quick stop returned false for %s axis slave=%s", axis_name, motor.slave)
        except Exception:
            logger.exception("quick stop failed for %s axis slave=%s", axis_name, motor.slave)


def _ensure_xy_ready(x_motor: MotorManager, y_motor: MotorManager) -> None:
    for axis_name, motor in (("x", x_motor), ("y", y_motor)):
        if not motor._ensure_mode_and_enable(MotorManager.MODE_PROFILE_POSITION, True):
            raise StageMotionError(f"{axis_name} axis cannot switch to PP mode and enable")


def _write_axis_pp_target(
    motor: MotorManager,
    *,
    target_pos: int,
    profile_vel: int,
    profile_acc: int,
    profile_dec: int,
    axis_name: str,
) -> None:
    client = motor.client
    slave = motor.slave
    if not client._write_32bit(slave, client.REG_PROFILE_VEL_HIGH, profile_vel):
        raise StageMotionError(f"{axis_name} axis failed to set profile velocity")
    if not client._write_32bit(slave, client.REG_PROFILE_ACC_HIGH, profile_acc):
        raise StageMotionError(f"{axis_name} axis failed to set profile acceleration")
    if not client._write_32bit(slave, client.REG_PROFILE_DEC_HIGH, profile_dec):
        raise StageMotionError(f"{axis_name} axis failed to set profile deceleration")
    if not client._write_32bit(slave, client.REG_TARGET_POS, target_pos):
        raise StageMotionError(f"{axis_name} axis failed to set target position")


def _trigger_axis_pp(motor: MotorManager, *, axis_name: str) -> None:
    client = motor.client
    slave = motor.slave
    if not client._write_controlword(slave, client.CMD_ENABLE_OPERATION):
        raise StageMotionError(f"{axis_name} axis failed to clear PP trigger bit")
    time.sleep(0.02)
    if not client._write_controlword(slave, client.CMD_ENABLE_OPERATION | 0x10):
        raise StageMotionError(f"{axis_name} axis failed to trigger PP move")


def _finish_axis_pp(motor: MotorManager, *, axis_name: str) -> None:
    client = motor.client
    slave = motor.slave
    if not client._restore_enabled_state(slave):
        raise StageMotionError(f"{axis_name} axis failed to restore enabled state")
    if not client.quick_stop(slave):
        raise StageMotionError(f"{axis_name} axis failed to quick stop after arrival")


def _snapshot_positions(x_motor: MotorManager, y_motor: MotorManager) -> Dict[str, int]:
    snapshot = snapshot_xy(x_motor, y_motor)
    return {
        "x": _require_axis_position(snapshot["x"], "x"),
        "y": _require_axis_position(snapshot["y"], "y"),
    }


def _snapshot_command_positions(x_motor: MotorManager, y_motor: MotorManager) -> Dict[str, int | None]:
    return {
        "x": x_motor.client._read_32bit(x_motor.slave, x_motor.client.REG_CMD_POS),
        "y": y_motor.client._read_32bit(y_motor.slave, y_motor.client.REG_CMD_POS),
    }


def _check_current_within_hard_limits(
    *,
    current: Mapping[str, int],
    stage_limits: Mapping[str, Any] | None,
) -> None:
    limits = _normalize_stage_limits(stage_limits)
    if not limits["enabled"]:
        return
    if int(current["x"]) < int(limits["x_min"]) or int(current["x"]) > int(limits["x_max"]):
        raise StageMotionError(
            f"x current position {int(current['x'])} is outside hard range "
            f"[{int(limits['x_min'])}, {int(limits['x_max'])}]"
        )
    if int(current["y"]) < int(limits["y_min"]) or int(current["y"]) > int(limits["y_max"]):
        raise StageMotionError(
            f"y current position {int(current['y'])} is outside hard range "
            f"[{int(limits['y_min'])}, {int(limits['y_max'])}]"
        )


def _check_axis_fault(motor: MotorManager, axis_name: str) -> None:
    status = motor.client._read_statusword(motor.slave)
    if status is not None and bool(status & motor.client.STAT_FAULT):
        raise StageMotionError(f"{axis_name} axis reported fault during move")


def _wait_xy_arrival(
    *,
    x_motor: MotorManager,
    y_motor: MotorManager,
    x_target: int,
    y_target: int,
    timeout_s: float,
    poll_s: float,
    monitor_tolerance: int,
    stage_limits: Mapping[str, Any] | None,
    stop_event: threading.Event | None = None,
    progress_callback: Callable[[Dict[str, int]], None] | None = None,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    poll_s = max(0.02, float(poll_s))
    last_pos = _snapshot_positions(x_motor, y_motor)
    while True:
        if stop_event is not None and stop_event.is_set():
            _quick_stop_xy(x_motor, y_motor)
            return {
                "current": last_pos,
                "stopped_by_request": True,
            }

        current = _snapshot_positions(x_motor, y_motor)
        last_pos = current
        _check_current_within_hard_limits(current=current, stage_limits=stage_limits)
        if progress_callback is not None:
            progress_callback(dict(current))
        if abs(current["x"] - x_target) <= monitor_tolerance and abs(current["y"] - y_target) <= monitor_tolerance:
            return {
                "current": current,
                "stopped_by_request": False,
            }
        _check_axis_fault(x_motor, "x")
        _check_axis_fault(y_motor, "y")
        if time.monotonic() >= deadline:
            raise StageMotionError(
                "xy move timed out: "
                f"target=({x_target},{y_target}), current=({last_pos['x']},{last_pos['y']}), timeout_s={timeout_s}"
            )
        time.sleep(poll_s)


def snapshot_axis(motor: MotorManager, axis_name: str) -> Dict[str, Any]:
    pos = motor.client._read_32bit(motor.slave, motor.client.REG_CURRENT_POS)
    sw = motor.client._read_statusword(motor.slave)
    return {
        "axis": axis_name,
        "slave": motor.slave,
        "current_pos": pos,
        "statusword": sw,
    }


def snapshot_xy(x_motor: MotorManager, y_motor: MotorManager) -> Dict[str, Any]:
    return {
        "x": snapshot_axis(x_motor, "x"),
        "y": snapshot_axis(y_motor, "y"),
    }


def move_to_absolute(
    *,
    port: str,
    x_target: int,
    y_target: int,
    profile_vel: int,
    profile_acc: int,
    profile_dec: int,
    x_slave: int = 1,
    y_slave: int = 2,
    baudrate: int = 115200,
    settle_s: float = 0.5,
    timeout_s: float = 120.0,
    poll_s: float = 0.05,
    arrival_tolerance_pulse: int | None = None,
    stage_limits: Mapping[str, Any] | None = None,
    stop_event: threading.Event | None = None,
    progress_callback: Callable[[Dict[str, int]], None] | None = None,
) -> Dict[str, Any]:
    """Move the XY stage to absolute pulse coordinates and return a motion snapshot."""
    port = str(port or "").strip()
    if not port:
        raise StageMotionError("port must not be empty")

    x_target = int(x_target)
    y_target = int(y_target)
    profile_vel = _positive_int(profile_vel, "profile_vel")
    profile_acc = _positive_int(profile_acc, "profile_acc")
    profile_dec = _positive_int(profile_dec, "profile_dec")
    x_slave = _positive_int(x_slave, "x_slave")
    y_slave = _positive_int(y_slave, "y_slave")
    baudrate = _positive_int(baudrate, "baudrate")
    settle_s = _non_negative_float(settle_s, "settle_s")
    timeout_s = _positive_float(timeout_s, "timeout_s")
    poll_s = _positive_float(poll_s, "poll_s")
    if arrival_tolerance_pulse is not None:
        arrival_tolerance_pulse = int(arrival_tolerance_pulse)
        if arrival_tolerance_pulse < 0:
            raise StageMotionError("arrival_tolerance_pulse must be non-negative")
    monitor_tolerance = 50 if arrival_tolerance_pulse is None else int(arrival_tolerance_pulse)

    _check_target_in_stage_limits(
        x_target=x_target,
        y_target=y_target,
        stage_limits=stage_limits,
        label="target",
    )

    x_motor: MotorManager | None = None
    y_motor: MotorManager | None = None
    with ModbusRTUClient(port=port, baudrate=baudrate) as client:
        x_motor = MotorManager(client, slave=x_slave)
        y_motor = MotorManager(client, slave=y_slave)
        try:
            before = snapshot_xy(x_motor, y_motor)
            _require_axis_position(before["x"], "x")
            _require_axis_position(before["y"], "y")

            _ensure_xy_ready(x_motor, y_motor)
            _write_axis_pp_target(
                x_motor,
                target_pos=x_target,
                profile_vel=profile_vel,
                profile_acc=profile_acc,
                profile_dec=profile_dec,
                axis_name="x",
            )
            _write_axis_pp_target(
                y_motor,
                target_pos=y_target,
                profile_vel=profile_vel,
                profile_acc=profile_acc,
                profile_dec=profile_dec,
                axis_name="y",
            )
            time.sleep(0.02)
            _trigger_axis_pp(x_motor, axis_name="x")
            _trigger_axis_pp(y_motor, axis_name="y")

            wait_result = _wait_xy_arrival(
                x_motor=x_motor,
                y_motor=y_motor,
                x_target=x_target,
                y_target=y_target,
                timeout_s=timeout_s,
                poll_s=poll_s,
                monitor_tolerance=monitor_tolerance,
                stage_limits=stage_limits,
                stop_event=stop_event,
                progress_callback=progress_callback,
            )
            if wait_result.get("stopped_by_request"):
                current = dict(wait_result.get("current") or {})
                return {
                    "target": {"x": x_target, "y": y_target},
                    "before": before,
                    "after": {
                        "x": {"axis": "x", "slave": x_slave, "current_pos": current.get("x"), "statusword": None},
                        "y": {"axis": "y", "slave": y_slave, "current_pos": current.get("y"), "statusword": None},
                    },
                    "err_to_target": {
                        "x": None if current.get("x") is None else int(current["x"]) - x_target,
                        "y": None if current.get("y") is None else int(current["y"]) - y_target,
                    },
                    "stopped_by_request": True,
                    "motion_params": {
                        "port": port,
                        "baudrate": baudrate,
                        "x_slave": x_slave,
                        "y_slave": y_slave,
                        "profile_vel": profile_vel,
                        "profile_acc": profile_acc,
                        "profile_dec": profile_dec,
                        "settle_s": settle_s,
                        "timeout_s": timeout_s,
                        "poll_s": poll_s,
                        "arrival_tolerance_pulse": arrival_tolerance_pulse,
                        "move_mode": "simultaneous_pp",
                    },
                }
            _finish_axis_pp(x_motor, axis_name="x")
            _finish_axis_pp(y_motor, axis_name="y")

            time.sleep(settle_s)
            after = snapshot_xy(x_motor, y_motor)
            after_x = _require_axis_position(after["x"], "x")
            after_y = _require_axis_position(after["y"], "y")
            cmd_pos = _snapshot_command_positions(x_motor, y_motor)

            err_to_target = {
                "x": after_x - x_target,
                "y": after_y - y_target,
            }
            _check_arrival_tolerance(
                err_to_target=err_to_target,
                arrival_tolerance_pulse=arrival_tolerance_pulse,
            )

            return {
                "target": {"x": x_target, "y": y_target},
                "before": before,
                "cmd_pos": cmd_pos,
                "move_result": {
                    "x_diff": None if cmd_pos["x"] is None else after_x - int(cmd_pos["x"]),
                    "y_diff": None if cmd_pos["y"] is None else after_y - int(cmd_pos["y"]),
                },
                "after": after,
                "err_to_target": err_to_target,
                "motion_params": {
                    "port": port,
                    "baudrate": baudrate,
                    "x_slave": x_slave,
                    "y_slave": y_slave,
                    "profile_vel": profile_vel,
                    "profile_acc": profile_acc,
                    "profile_dec": profile_dec,
                    "settle_s": settle_s,
                    "timeout_s": timeout_s,
                    "poll_s": poll_s,
                    "arrival_tolerance_pulse": arrival_tolerance_pulse,
                    "move_mode": "simultaneous_pp",
                },
            }
        except Exception:
            _quick_stop_xy(x_motor, y_motor)
            raise


def move_to_absolute_with_approach(
    *,
    port: str,
    x_target: int,
    y_target: int,
    profile_vel: int,
    profile_acc: int,
    profile_dec: int,
    x_slave: int = 1,
    y_slave: int = 2,
    baudrate: int = 115200,
    settle_s: float = 0.5,
    timeout_s: float = 120.0,
    poll_s: float = 0.05,
    arrival_tolerance_pulse: int | None = None,
    stage_limits: Mapping[str, Any] | None = None,
    stop_event: threading.Event | None = None,
    progress_callback: Callable[[Dict[str, int]], None] | None = None,
    approach_cfg: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Move to a target using an optional fixed-direction final approach."""
    cfg = approach_cfg or {}
    if not bool(cfg.get("enabled", False)):
        return move_to_absolute(
            port=port,
            x_target=x_target,
            y_target=y_target,
            profile_vel=profile_vel,
            profile_acc=profile_acc,
            profile_dec=profile_dec,
            x_slave=x_slave,
            y_slave=y_slave,
            baudrate=baudrate,
            settle_s=settle_s,
            timeout_s=timeout_s,
            poll_s=poll_s,
            arrival_tolerance_pulse=arrival_tolerance_pulse,
            stage_limits=stage_limits,
            stop_event=stop_event,
            progress_callback=progress_callback,
        )

    x_direction = int(cfg.get("x_direction", cfg.get("direction", 1)))
    y_direction = int(cfg.get("y_direction", cfg.get("direction", 1)))
    if x_direction not in (-1, 1) or y_direction not in (-1, 1):
        raise StageMotionError("approach x_direction/y_direction must be +1 or -1")

    default_margin = int(cfg.get("margin_pulse", 0))
    x_margin = int(cfg.get("x_margin_pulse", default_margin))
    y_margin = int(cfg.get("y_margin_pulse", default_margin))
    if x_margin < 0 or y_margin < 0:
        raise StageMotionError("approach margin_pulse must be non-negative")

    pre_x = int(x_target) - x_direction * x_margin
    pre_y = int(y_target) - y_direction * y_margin
    _check_target_in_stage_limits(
        x_target=pre_x,
        y_target=pre_y,
        stage_limits=stage_limits,
        label="approach.pre_target",
    )
    _check_target_in_stage_limits(
        x_target=int(x_target),
        y_target=int(y_target),
        stage_limits=stage_limits,
        label="target",
    )

    pre_move = move_to_absolute(
        port=port,
        x_target=pre_x,
        y_target=pre_y,
        profile_vel=profile_vel,
        profile_acc=profile_acc,
        profile_dec=profile_dec,
        x_slave=x_slave,
        y_slave=y_slave,
        baudrate=baudrate,
        settle_s=float(cfg.get("pre_settle_s", settle_s)),
        timeout_s=timeout_s,
        poll_s=poll_s,
        arrival_tolerance_pulse=arrival_tolerance_pulse,
        stage_limits=stage_limits,
        stop_event=stop_event,
        progress_callback=progress_callback,
    )
    if pre_move.get("stopped_by_request"):
        return {
            **pre_move,
            "approach": {
                "enabled": True,
                "x_direction": x_direction,
                "y_direction": y_direction,
                "x_margin_pulse": x_margin,
                "y_margin_pulse": y_margin,
                "pre_target": {"x": pre_x, "y": pre_y},
                "pre_move": pre_move,
                "final_move": None,
            },
        }
    final_move = move_to_absolute(
        port=port,
        x_target=x_target,
        y_target=y_target,
        profile_vel=profile_vel,
        profile_acc=profile_acc,
        profile_dec=profile_dec,
        x_slave=x_slave,
        y_slave=y_slave,
        baudrate=baudrate,
        settle_s=settle_s,
        timeout_s=timeout_s,
        poll_s=poll_s,
        arrival_tolerance_pulse=arrival_tolerance_pulse,
        stage_limits=stage_limits,
        stop_event=stop_event,
        progress_callback=progress_callback,
    )

    return {
        **final_move,
        "approach": {
            "enabled": True,
            "x_direction": x_direction,
            "y_direction": y_direction,
            "x_margin_pulse": x_margin,
            "y_margin_pulse": y_margin,
            "pre_target": {"x": pre_x, "y": pre_y},
            "pre_move": pre_move,
            "final_move": final_move,
        },
    }
