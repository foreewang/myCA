"""封装 XY 位移台绝对运动、接近运动、限位检查和到位校验。"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Mapping

from devices.motion.communication_timing import motion_measure as measure

from devices.motion.MotorManager import MotorManager
from devices.motion.modbus import ModbusRTUClient, validate_slave_switch_gap_ms

logger = logging.getLogger(__name__)

# After commanding one RS-485 slave, wait before addressing the other. USB
# adapters and the Y drive commonly drop the next frame during X current spikes.
_RS485_SLAVE_GAP_S = 0.05
_PP_NEW_SETPOINT_BIT = 0x10


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
    axes = (("x", x_motor), ("y", y_motor))
    last_index = len(axes) - 1
    for index, (axis_name, motor) in enumerate(axes):
        if not motor._ensure_mode_and_enable(MotorManager.MODE_PROFILE_POSITION, True):
            raise StageMotionError(f"{axis_name} axis cannot switch to PP mode and enable")
        if index != last_index:
            time.sleep(_RS485_SLAVE_GAP_S)


def _write_axis_pp_target(
    motor: MotorManager,
    *,
    target_pos: int,
    profile_vel: int,
    profile_acc: int,
    profile_dec: int,
    axis_name: str,
    profile_cache=None,
) -> None:
    client = motor.client
    slave = motor.slave
    generation = getattr(client, "configuration_generation", {}).get(slave, 0)
    profile = (profile_vel, profile_acc, profile_dec, generation)
    if profile_cache is None or profile_cache.get(slave) != profile:
        if profile_cache is not None:
            profile_cache.pop(slave, None)
        if not client._write_32bit(slave, client.REG_PROFILE_VEL_HIGH, profile_vel):
            raise StageMotionError(f"{axis_name} axis failed to set profile velocity")
        if not client._write_32bit(slave, client.REG_PROFILE_ACC_HIGH, profile_acc):
            raise StageMotionError(f"{axis_name} axis failed to set profile acceleration")
        if not client._write_32bit(slave, client.REG_PROFILE_DEC_HIGH, profile_dec):
            raise StageMotionError(f"{axis_name} axis failed to set profile deceleration")
        if profile_cache is not None:
            profile_cache[slave] = profile
    if not client._write_32bit(slave, client.REG_TARGET_POS, target_pos):
        raise StageMotionError(f"{axis_name} axis failed to set target position")


def _write_axis_controlword(motor: MotorManager, value: int, *, axis_name: str, action: str) -> None:
    client = motor.client
    slave = motor.slave
    if not client._write_controlword(slave, value):
        raise StageMotionError(
            f"{axis_name} axis failed to {action} "
            f"(controlword=0x{int(value):02X}, slave={slave})"
        )


def _trigger_xy_pp(x_motor: MotorManager, y_motor: MotorManager) -> None:
    """Pulse PP new-setpoint on X and Y without starting X before Y is addressed.

    CiA 402 needs a 0→1 edge on bit 4. Clearing both axes first, then setting
    both, keeps the move closer to simultaneous and avoids commanding X into
    motion (current spike) before the next Y controlword write.
    """
    enable = x_motor.client.CMD_ENABLE_OPERATION
    trigger = enable | _PP_NEW_SETPOINT_BIT
    _write_axis_controlword(x_motor, enable, axis_name="x", action="clear PP trigger bit")
    time.sleep(_RS485_SLAVE_GAP_S)
    _write_axis_controlword(y_motor, enable, axis_name="y", action="clear PP trigger bit")
    time.sleep(0.02)
    _write_axis_controlword(x_motor, trigger, axis_name="x", action="trigger PP move")
    time.sleep(_RS485_SLAVE_GAP_S)
    _write_axis_controlword(y_motor, trigger, axis_name="y", action="trigger PP move")


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


def _checked_snapshot_positions(snapshot):
    positions = {}
    for axis in ("x", "y"):
        positions[axis] = _require_axis_position(snapshot[axis], axis)
        status = snapshot[axis].get("statusword")
        if status is None:
            raise StageMotionError(f"failed to read {axis} statusword")
        if status & ModbusRTUClient.STAT_FAULT:
            raise StageMotionError(f"{axis} axis reported fault during move")
    return positions


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
    reuse_status: bool = False,
    require_standstill: bool = False,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    poll_s = max(0.02, float(poll_s))
    last_pos = {} if reuse_status else _snapshot_positions(x_motor, y_motor)
    stable_since = None
    stable_min = {}
    stable_max = {}
    stable_samples = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            _quick_stop_xy(x_motor, y_motor)
            return {
                "current": last_pos,
                "stopped_by_request": True,
            }

        snapshot = snapshot_xy(x_motor, y_motor) if reuse_status else None
        current = _checked_snapshot_positions(snapshot) if reuse_status else _snapshot_positions(x_motor, y_motor)
        last_pos = current
        _check_current_within_hard_limits(current=current, stage_limits=stage_limits)
        if progress_callback is not None:
            progress_callback(dict(current))
        within_tolerance = abs(current["x"] - x_target) <= monitor_tolerance and abs(current["y"] - y_target) <= monitor_tolerance
        arrived = within_tolerance
        if require_standstill:
            # Experimental hold mode: require enabled + target reached and a
            # <=10 pulse position range over >=150ms / >=3 consecutive samples.
            mask = ModbusRTUClient.STAT_TARGET_REACHED | ModbusRTUClient.STAT_OPERATION_ENABLED
            valid = within_tolerance and all(snapshot[a]["statusword"] & mask == mask for a in ("x", "y"))
            now = time.monotonic()
            if not valid:
                stable_since = None
                stable_samples = 0
            else:
                if stable_since is None:
                    stable_since = now
                    stable_min = dict(current)
                    stable_max = dict(current)
                    stable_samples = 0
                for axis in ("x", "y"):
                    stable_min[axis] = min(stable_min[axis], current[axis])
                    stable_max[axis] = max(stable_max[axis], current[axis])
                if any(stable_max[a] - stable_min[a] > 10 for a in ("x", "y")):
                    stable_since = now
                    stable_min = dict(current)
                    stable_max = dict(current)
                    stable_samples = 0
                stable_samples += 1
            arrived = valid and stable_samples >= 3 and now - stable_since >= 0.15
        if arrived:
            return {
                "current": current,
                "stopped_by_request": False,
                "standstill_verified": require_standstill,
                "stable_samples": stable_samples,
            }
        if not reuse_status:
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
    timings_ms: Dict[str, float] | None = None,
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
    session=None,
    release_after_move: bool = False,
    keep_enabled: bool = False,
    slave_switch_gap_ms: float = 0.0,
) -> Dict[str, Any]:
    """Move the XY stage to absolute pulse coordinates and return a motion snapshot."""
    timings = timings_ms if timings_ms is not None else {}
    slave_switch_gap_ms = validate_slave_switch_gap_ms(slave_switch_gap_ms)
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
    if keep_enabled and (session is None or not session.options["hold_enabled"] or release_after_move):
        raise StageMotionError("keep_enabled requires an active hold session without release")

    _check_target_in_stage_limits(
        x_target=x_target,
        y_target=y_target,
        stage_limits=stage_limits,
        label="target",
    )

    x_motor: MotorManager | None = None
    y_motor: MotorManager | None = None
    gap_options = {"slave_switch_gap_ms": slave_switch_gap_ms} if slave_switch_gap_ms else {}
    connection = (session.connection(ModbusRTUClient, port=port, baudrate=baudrate,
                                    x_slave=x_slave, y_slave=y_slave, release=release_after_move,
                                    slave_switch_gap_ms=slave_switch_gap_ms)
                  if session is not None else ModbusRTUClient(port=port, baudrate=baudrate, **gap_options))
    with connection as client:
        x_motor = MotorManager(client, slave=x_slave)
        y_motor = MotorManager(client, slave=y_slave)
        try:
            with measure(timings, "position_snapshot"):
                before = snapshot_xy(x_motor, y_motor)
            _require_axis_position(before["x"], "x")
            _require_axis_position(before["y"], "y")
            if session is not None:
                _checked_snapshot_positions(before)
                session.needs_stop = True
            profile_cache = session.profiles if session is not None and session.options["reuse_profile"] else None

            with measure(timings, "axis_ready"):
                _ensure_xy_ready(x_motor, y_motor)
            with measure(timings, "write_targets"):
                _write_axis_pp_target(
                    x_motor,
                    target_pos=x_target,
                    profile_vel=profile_vel,
                    profile_acc=profile_acc,
                    profile_dec=profile_dec,
                    axis_name="x",
                    profile_cache=profile_cache,
                )
            with measure(timings, "command_gap"):
                time.sleep(_RS485_SLAVE_GAP_S)
            with measure(timings, "write_targets"):
                _write_axis_pp_target(
                    y_motor,
                    target_pos=y_target,
                    profile_vel=profile_vel,
                    profile_acc=profile_acc,
                    profile_dec=profile_dec,
                    axis_name="y",
                    profile_cache=profile_cache,
                )
            with measure(timings, "command_gap"):
                time.sleep(0.02)
            with measure(timings, "trigger"):
                _trigger_xy_pp(x_motor, y_motor)

            with measure(timings, "wait_arrival"):
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
                    reuse_status=session is not None,
                    require_standstill=keep_enabled,
                )
            if wait_result.get("stopped_by_request"):
                current = dict(wait_result.get("current") or {})
                return {
                    "timings_ms": timings,
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
                        "slave_switch_gap_ms": slave_switch_gap_ms,
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
            with measure(timings, "finish_control"):
                if keep_enabled:
                    if not client._restore_enabled_state(x_slave):
                        raise StageMotionError("x axis failed to retain enabled state")
                else:
                    _finish_axis_pp(x_motor, axis_name="x")
            with measure(timings, "finish_control"):
                if keep_enabled:
                    if not client._restore_enabled_state(y_slave):
                        raise StageMotionError("y axis failed to retain enabled state")
                else:
                    _finish_axis_pp(y_motor, axis_name="y")
            if session is not None:
                session.needs_stop = keep_enabled

            with measure(timings, "settle"):
                time.sleep(settle_s)
            with measure(timings, "position_snapshot"):
                after = snapshot_xy(x_motor, y_motor)
            after_x = _require_axis_position(after["x"], "x")
            after_y = _require_axis_position(after["y"], "y")
            if session is not None:
                _checked_snapshot_positions(after)
            if keep_enabled:
                mask = ModbusRTUClient.STAT_TARGET_REACHED | ModbusRTUClient.STAT_OPERATION_ENABLED
                if any(after[a]["statusword"] & mask != mask or abs(after[a]["current_pos"] - wait_result["current"][a]) > 10 for a in ("x", "y")):
                    raise StageMotionError("position/status changed after hold standstill check")
            with measure(timings, "command_snapshot"):
                cmd_pos = _snapshot_command_positions(x_motor, y_motor)

            err_to_target = {
                "x": after_x - x_target,
                "y": after_y - y_target,
            }
            with measure(timings, "arrival_check"):
                _check_arrival_tolerance(
                    err_to_target=err_to_target,
                    arrival_tolerance_pulse=arrival_tolerance_pulse,
                )

            return {
                "timings_ms": timings,
                "target": {"x": x_target, "y": y_target},
                "before": before,
                "cmd_pos": cmd_pos,
                "move_result": {
                    "x_diff": None if cmd_pos["x"] is None else after_x - int(cmd_pos["x"]),
                    "y_diff": None if cmd_pos["y"] is None else after_y - int(cmd_pos["y"]),
                },
                "after": after,
                "err_to_target": err_to_target,
                "stop_shoot": {"optimized": session is not None, "held_enabled": keep_enabled,
                               "standstill_verified": wait_result.get("standstill_verified", False),
                               "stable_samples": wait_result.get("stable_samples", 0)},
                "motion_params": {
                    "port": port,
                    "baudrate": baudrate,
                    "slave_switch_gap_ms": slave_switch_gap_ms,
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
