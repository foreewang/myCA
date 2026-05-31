from __future__ import annotations

import threading
import time
from typing import Any, Dict, Iterable

from devices.motion.MotorManager import MotorManager
from devices.motion.modbus import ModbusRTUClient


class StageReciprocationError(RuntimeError):
    pass


class StageReciprocationController:
    """Run XY stage point-to-point reciprocation in a background thread."""

    DEFAULT_POINT_A = {"x": 0, "y": 7500000}
    DEFAULT_POINT_B = {"x": 8865800, "y": 6185500}
    DEFAULT_LIMITS = {
        "x_min": -800000,
        "x_max": 10400000,
        "y_min": -8900000,
        "y_max": 7700000,
        "safety_margin": 147500,
    }

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: Dict[str, Any] = {
            "status": "stopped",
            "message": "not running",
        }

    def start(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise StageReciprocationError("位移台往复运动已经在运行")

            normalized = self._normalize_cfg(cfg)
            self._stop_event.clear()
            self._status = {
                "status": "starting",
                "message": "starting point-to-point reciprocation",
                "config": normalized,
                "started_at": time.time(),
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(normalized,),
                name="stage-reciprocation",
                daemon=True,
            )
            self._thread.start()
            return dict(self._status)

    def stop(self, join_timeout_s: float = 5.0) -> Dict[str, Any]:
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._status = {
                    **self._status,
                    "status": "stopped",
                    "message": "not running",
                    "stopped_at": time.time(),
                }
                return dict(self._status)

            self._stop_event.set()

        thread.join(timeout=max(0.0, float(join_timeout_s)))
        with self._lock:
            if thread.is_alive():
                self._status = {
                    **self._status,
                    "status": "stopping",
                    "message": "stop requested; waiting for motor quick stop",
                }
            return dict(self._status)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            thread_alive = self._thread is not None and self._thread.is_alive()
            if not thread_alive and self._status.get("status") in {"starting", "running", "moving", "stopping"}:
                self._status = {
                    **self._status,
                    "status": "stopped",
                    "message": "thread exited",
                }
            return dict(self._status)

    def _normalize_cfg(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        point_a = {
            "x": int(cfg.get("point_a_x", self.DEFAULT_POINT_A["x"])),
            "y": int(cfg.get("point_a_y", self.DEFAULT_POINT_A["y"])),
        }
        point_b = {
            "x": int(cfg.get("point_b_x", self.DEFAULT_POINT_B["x"])),
            "y": int(cfg.get("point_b_y", self.DEFAULT_POINT_B["y"])),
        }
        if point_a == point_b:
            raise StageReciprocationError("往复运动的两个点位不能相同")

        limits = {
            "enabled": bool(cfg.get("limit_check_enabled", True)),
            "x_min": int(cfg.get("x_min", self.DEFAULT_LIMITS["x_min"])),
            "x_max": int(cfg.get("x_max", self.DEFAULT_LIMITS["x_max"])),
            "y_min": int(cfg.get("y_min", self.DEFAULT_LIMITS["y_min"])),
            "y_max": int(cfg.get("y_max", self.DEFAULT_LIMITS["y_max"])),
            "safety_margin": int(cfg.get("safety_margin", self.DEFAULT_LIMITS["safety_margin"])),
        }
        if limits["enabled"]:
            if limits["x_min"] >= limits["x_max"] or limits["y_min"] >= limits["y_max"]:
                raise StageReciprocationError("限位配置错误：min 必须小于 max")
            if limits["safety_margin"] < 0:
                raise StageReciprocationError("safety_margin 不能为负数")
            self._validate_target_in_safe_range(point_a, limits, "point_a")
            self._validate_target_in_safe_range(point_b, limits, "point_b")

        max_cycles_raw = cfg.get("max_cycles")
        max_cycles = None if max_cycles_raw is None else int(max_cycles_raw)
        if max_cycles is not None and max_cycles <= 0:
            raise StageReciprocationError("max_cycles 必须大于 0，或不传表示持续运行")

        return {
            "port": str(cfg.get("port", "COM3")),
            "baudrate": int(cfg.get("baudrate", 115200)),
            "x_slave": int(cfg.get("x_slave", 1)),
            "y_slave": int(cfg.get("y_slave", 2)),
            "point_a": point_a,
            "point_b": point_b,
            "profile_vel": int(cfg.get("profile_vel", 500000)),
            "profile_acc": int(cfg.get("profile_acc", 100000)),
            "profile_dec": int(cfg.get("profile_dec", 100000)),
            "arrival_tolerance": int(cfg.get("arrival_tolerance", 80)),
            "poll_s": float(cfg.get("poll_s", 0.05)),
            "settle_s": float(cfg.get("settle_s", 0.2)),
            "move_timeout_s": float(cfg.get("move_timeout_s", 120.0)),
            "max_cycles": max_cycles,
            "limits": limits,
        }

    def _run(self, cfg: Dict[str, Any]) -> None:
        cycles = 0
        target_index = 0
        targets = [cfg["point_a"], cfg["point_b"]]
        try:
            with ModbusRTUClient(port=cfg["port"], baudrate=cfg["baudrate"]) as client:
                motors = {
                    "x": MotorManager(client, slave=cfg["x_slave"]),
                    "y": MotorManager(client, slave=cfg["y_slave"]),
                }
                self._ensure_xy_ready(motors)
                self._validate_current_within_hard_limits(self._snapshot_positions(motors), cfg)

                with self._lock:
                    self._status = {
                        **self._status,
                        "status": "running",
                        "message": "point-to-point reciprocation running",
                        "cycle": cycles,
                        "current_pos": self._snapshot_positions(motors),
                    }

                while not self._stop_event.is_set():
                    target = targets[target_index]
                    move = self._move_xy_interruptible(motors, target, cfg, cycles)
                    if move.get("stopped_by_request"):
                        break
                    if self._stop_event.is_set():
                        break
                    target_index = 1 - target_index
                    if target_index == 0:
                        cycles += 1
                        if cfg["max_cycles"] is not None and cycles >= cfg["max_cycles"]:
                            break
                    with self._lock:
                        self._status = {
                            **self._status,
                            "last_move": move,
                            "cycle": cycles,
                        }

                self._quick_stop_motors(motors.values())
                with self._lock:
                    self._status = {
                        **self._status,
                        "status": "stopped",
                        "message": "reciprocation stopped",
                        "cycle": cycles,
                        "current_pos": self._snapshot_positions(motors),
                        "stopped_at": time.time(),
                    }
        except Exception as exc:
            with self._lock:
                self._status = {
                    **self._status,
                    "status": "failed",
                    "message": str(exc),
                    "error": str(exc),
                    "stopped_at": time.time(),
                }

    def _ensure_xy_ready(self, motors: Dict[str, MotorManager]) -> None:
        for axis in ("x", "y"):
            if not motors[axis]._ensure_mode_and_enable(MotorManager.MODE_PROFILE_POSITION, True):
                raise StageReciprocationError(f"{axis} 轴无法切换到 PP 模式并使能")

    def _move_xy_interruptible(
        self,
        motors: Dict[str, MotorManager],
        target: Dict[str, int],
        cfg: Dict[str, Any],
        cycle: int,
    ) -> Dict[str, Any]:
        before = self._snapshot_positions(motors)
        self._validate_target_in_safe_range(target, cfg["limits"], "target")
        with self._lock:
            self._status = {
                **self._status,
                "status": "moving",
                "message": "moving stage between reciprocation points",
                "cycle": cycle,
                "target": dict(target),
                "current_pos": before,
            }

        self._ensure_xy_ready(motors)
        self._start_axis_pp(motors["x"], int(target["x"]), cfg)
        self._start_axis_pp(motors["y"], int(target["y"]), cfg)

        deadline = time.monotonic() + float(cfg["move_timeout_s"])
        poll_s = max(0.02, float(cfg["poll_s"]))
        tolerance = abs(int(cfg["arrival_tolerance"]))
        while True:
            if self._stop_event.is_set():
                self._quick_stop_motors(motors.values())
                after_stop = self._snapshot_positions(motors)
                return {
                    "target": dict(target),
                    "before": before,
                    "after": after_stop,
                    "stopped_by_request": True,
                    "err_to_target": {
                        "x": int(after_stop["x"] - int(target["x"])),
                        "y": int(after_stop["y"] - int(target["y"])),
                    },
                }

            current = self._snapshot_positions(motors)
            self._validate_current_within_hard_limits(current, cfg)
            with self._lock:
                self._status = {
                    **self._status,
                    "current_pos": current,
                }

            if (
                abs(current["x"] - int(target["x"])) <= tolerance
                and abs(current["y"] - int(target["y"])) <= tolerance
            ):
                break
            if time.monotonic() >= deadline:
                self._quick_stop_motors(motors.values())
                raise StageReciprocationError(f"移动到目标点超时: {target}")
            time.sleep(poll_s)

        self._finish_axis_pp(motors["x"])
        self._finish_axis_pp(motors["y"])
        time.sleep(max(0.0, float(cfg["settle_s"])))
        after = self._snapshot_positions(motors)
        return {
            "target": dict(target),
            "before": before,
            "after": after,
            "cmd_pos": self._snapshot_command_positions(motors),
            "err_to_target": {
                "x": int(after["x"] - int(target["x"])),
                "y": int(after["y"] - int(target["y"])),
            },
        }

    def _start_axis_pp(self, motor: MotorManager, target_pos: int, cfg: Dict[str, Any]) -> None:
        client = motor.client
        slave = motor.slave
        if not client._write_32bit(slave, client.REG_PROFILE_VEL_HIGH, int(cfg["profile_vel"])):
            raise StageReciprocationError(f"从站 {slave} 设置轮廓速度失败")
        if not client._write_32bit(slave, client.REG_PROFILE_ACC_HIGH, int(cfg["profile_acc"])):
            raise StageReciprocationError(f"从站 {slave} 设置轮廓加速度失败")
        if not client._write_32bit(slave, client.REG_PROFILE_DEC_HIGH, int(cfg["profile_dec"])):
            raise StageReciprocationError(f"从站 {slave} 设置轮廓减速度失败")
        if not client._write_32bit(slave, client.REG_TARGET_POS, int(target_pos)):
            raise StageReciprocationError(f"从站 {slave} 设置目标位置失败")
        time.sleep(0.02)
        if not client._write_controlword(slave, client.CMD_ENABLE_OPERATION):
            raise StageReciprocationError(f"从站 {slave} 清除 PP 触发位失败")
        time.sleep(0.02)
        if not client._write_controlword(slave, client.CMD_ENABLE_OPERATION | 0x10):
            raise StageReciprocationError(f"从站 {slave} 触发 PP 运动失败")

    def _finish_axis_pp(self, motor: MotorManager) -> None:
        client = motor.client
        slave = motor.slave
        if not client._restore_enabled_state(slave):
            raise StageReciprocationError(f"从站 {slave} 到位后恢复使能失败")
        if not client.quick_stop(slave):
            raise StageReciprocationError(f"从站 {slave} 到位后停止失败")

    def _quick_stop_motors(self, motors: Iterable[MotorManager]) -> None:
        for motor in motors:
            try:
                motor.vl_stop()
            except Exception:
                pass

    def _snapshot_positions(self, motors: Dict[str, MotorManager]) -> Dict[str, int]:
        out = {}
        for axis in ("x", "y"):
            pos = motors[axis].client._read_32bit(
                motors[axis].slave,
                motors[axis].client.REG_CURRENT_POS,
            )
            if pos is None:
                raise StageReciprocationError(f"无法读取 {axis} 轴当前位置")
            out[axis] = int(pos)
        return out

    def _snapshot_command_positions(self, motors: Dict[str, MotorManager]) -> Dict[str, int | None]:
        out = {}
        for axis in ("x", "y"):
            out[axis] = motors[axis].client._read_32bit(
                motors[axis].slave,
                motors[axis].client.REG_CMD_POS,
            )
        return out

    def _safe_bounds(self, limits: Dict[str, Any], axis: str) -> tuple[int, int]:
        margin = int(limits["safety_margin"])
        return int(limits[f"{axis}_min"]) + margin, int(limits[f"{axis}_max"]) - margin

    def _hard_bounds(self, limits: Dict[str, Any], axis: str) -> tuple[int, int]:
        return int(limits[f"{axis}_min"]), int(limits[f"{axis}_max"])

    def _validate_target_in_safe_range(self, target: Dict[str, int], limits: Dict[str, Any], name: str) -> None:
        if not limits["enabled"]:
            return
        for axis in ("x", "y"):
            lo, hi = self._safe_bounds(limits, axis)
            value = int(target[axis])
            if value < lo or value > hi:
                raise StageReciprocationError(f"{name}.{axis}={value} 超出安全范围 [{lo}, {hi}]")

    def _validate_current_within_hard_limits(self, positions: Dict[str, int], cfg: Dict[str, Any]) -> None:
        limits = cfg["limits"]
        if not limits["enabled"]:
            return
        for axis, pos in positions.items():
            lo, hi = self._hard_bounds(limits, axis)
            if int(pos) < lo or int(pos) > hi:
                raise StageReciprocationError(f"{axis} 轴当前位置 {pos} 已超出机械限位 [{lo}, {hi}]")


stage_reciprocation_controller = StageReciprocationController()
