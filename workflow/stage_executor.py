from __future__ import annotations

import logging
import time
from typing import Any, Dict, Mapping

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
    settle_s: float = 0.8,
    timeout_s: float = 120.0,
    arrival_tolerance_pulse: int | None = None,
    stage_limits: Mapping[str, Any] | None = None,
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
    if arrival_tolerance_pulse is not None:
        arrival_tolerance_pulse = int(arrival_tolerance_pulse)
        if arrival_tolerance_pulse < 0:
            raise StageMotionError("arrival_tolerance_pulse must be non-negative")

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

            x_diff = x_motor.pp_absolute_move(
                target_pos=x_target,
                profile_vel=profile_vel,
                profile_acc=profile_acc,
                profile_dec=profile_dec,
                timeout=timeout_s,
            )
            if x_diff is None:
                raise StageMotionError(f"x axis failed to move to {x_target}")

            y_diff = y_motor.pp_absolute_move(
                target_pos=y_target,
                profile_vel=profile_vel,
                profile_acc=profile_acc,
                profile_dec=profile_dec,
                timeout=timeout_s,
            )
            if y_diff is None:
                raise StageMotionError(f"y axis failed to move to {y_target}")

            time.sleep(settle_s)
            after = snapshot_xy(x_motor, y_motor)
            after_x = _require_axis_position(after["x"], "x")
            after_y = _require_axis_position(after["y"], "y")

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
                "move_result": {"x_diff": int(x_diff), "y_diff": int(y_diff)},
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
                    "arrival_tolerance_pulse": arrival_tolerance_pulse,
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
    settle_s: float = 0.8,
    timeout_s: float = 120.0,
    arrival_tolerance_pulse: int | None = None,
    stage_limits: Mapping[str, Any] | None = None,
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
            arrival_tolerance_pulse=arrival_tolerance_pulse,
            stage_limits=stage_limits,
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
        arrival_tolerance_pulse=arrival_tolerance_pulse,
        stage_limits=stage_limits,
    )
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
        arrival_tolerance_pulse=arrival_tolerance_pulse,
        stage_limits=stage_limits,
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
