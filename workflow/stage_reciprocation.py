from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable

from devices.motion.MotorManager import MotorManager
from devices.motion.modbus import ModbusRTUClient
from workflow.config_loader import load_yaml
from workflow.plate_geometry import compute_well_start


class StageReciprocationError(RuntimeError):
    pass


class StageReciprocationController:
    """Run the stage through a fixed 24-well scan path in a background thread."""

    DEFAULT_PLATE_TYPE = "24-well"
    DEFAULT_SCAN_WELLS = ["B2", "B3", "B4", "C2", "C3", "C4"]
    DEFAULT_PLATES_PATH = Path(__file__).resolve().parent.parent / "config" / "plates.yaml"
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
                raise StageReciprocationError("stage scan is already running")

            normalized = self._normalize_cfg(cfg)
            self._stop_event.clear()
            self._status = {
                "status": "starting",
                "message": "starting 24-well stage scan",
                "config": normalized,
                "started_at": time.time(),
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(normalized,),
                name="stage-scan",
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
        targets = self._build_scan_targets(cfg)
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
                raise StageReciprocationError("invalid limits: min must be smaller than max")
            if limits["safety_margin"] < 0:
                raise StageReciprocationError("safety_margin must be non-negative")
            for target in targets:
                self._validate_target_in_safe_range(target, limits, str(target["well_name"]))

        max_cycles_raw = cfg.get("max_cycles")
        max_cycles = None if max_cycles_raw is None else int(max_cycles_raw)
        if max_cycles is not None and max_cycles <= 0:
            raise StageReciprocationError("max_cycles must be positive, or omitted to run until stop")

        return {
            "port": str(cfg.get("port", "COM3")),
            "baudrate": int(cfg.get("baudrate", 115200)),
            "x_slave": int(cfg.get("x_slave", 1)),
            "y_slave": int(cfg.get("y_slave", 2)),
            "plate_type": self.DEFAULT_PLATE_TYPE,
            "scan_wells": list(self.DEFAULT_SCAN_WELLS),
            "targets": targets,
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

    def _build_scan_targets(self, cfg: Dict[str, Any]) -> list[Dict[str, Any]]:
        plates_path = Path(str(cfg.get("plates_path") or self.DEFAULT_PLATES_PATH))
        if not plates_path.exists():
            raise StageReciprocationError(f"plates config not found: {plates_path}")

        plates_cfg = load_yaml(plates_path)
        plate = (plates_cfg.get("plates") or {}).get(self.DEFAULT_PLATE_TYPE)
        if not isinstance(plate, dict):
            raise StageReciprocationError(f"missing plate config: {self.DEFAULT_PLATE_TYPE}")

        targets: list[Dict[str, Any]] = []
        for index, well_name in enumerate(self.DEFAULT_SCAN_WELLS, start=1):
            pos = compute_well_start(plate, well_name)
            targets.append(
                {
                    "index": index,
                    "well_name": well_name,
                    "x": int(pos["x"]),
                    "y": int(pos["y"]),
                }
            )
        return targets

    def _run(self, cfg: Dict[str, Any]) -> None:
        cycles = 0
        target_index = 0
        targets = list(cfg["targets"])
        if not targets:
            raise StageReciprocationError("scan target list is empty")

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
                        "message": "24-well stage scan running",
                        "cycle": cycles,
                        "target_count": len(targets),
                        "completed_targets": 0,
                        "current_pos": self._snapshot_positions(motors),
                    }

                while not self._stop_event.is_set():
                    target = targets[target_index]
                    move = self._move_xy_interruptible(motors, target, cfg, cycles)
                    if move.get("stopped_by_request") or self._stop_event.is_set():
                        break

                    target_index += 1
                    if target_index >= len(targets):
                        target_index = 0
                        cycles += 1
                        if cfg["max_cycles"] is not None and cycles >= cfg["max_cycles"]:
                            break

                    with self._lock:
                        self._status = {
                            **self._status,
                            "last_move": move,
                            "cycle": cycles,
                            "completed_targets": cycles * len(targets) + target_index,
                            "next_target": dict(targets[target_index]),
                        }

                self._quick_stop_motors(motors.values())
                with self._lock:
                    self._status = {
                        **self._status,
                        "status": "stopped",
                        "message": "stage scan stopped",
                        "cycle": cycles,
                        "completed_targets": cycles * len(targets) + target_index,
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
                raise StageReciprocationError(f"{axis} axis cannot switch to PP mode and enable")

    def _move_xy_interruptible(
        self,
        motors: Dict[str, MotorManager],
        target: Dict[str, Any],
        cfg: Dict[str, Any],
        cycle: int,
    ) -> Dict[str, Any]:
        before = self._snapshot_positions(motors)
        self._validate_target_in_safe_range(target, cfg["limits"], str(target.get("well_name") or "target"))
        with self._lock:
            self._status = {
                **self._status,
                "status": "moving",
                "message": "moving stage to scan well",
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
                raise StageReciprocationError(f"move to target timed out: {target}")
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
            raise StageReciprocationError(f"slave {slave} failed to set profile velocity")
        if not client._write_32bit(slave, client.REG_PROFILE_ACC_HIGH, int(cfg["profile_acc"])):
            raise StageReciprocationError(f"slave {slave} failed to set profile acceleration")
        if not client._write_32bit(slave, client.REG_PROFILE_DEC_HIGH, int(cfg["profile_dec"])):
            raise StageReciprocationError(f"slave {slave} failed to set profile deceleration")
        if not client._write_32bit(slave, client.REG_TARGET_POS, int(target_pos)):
            raise StageReciprocationError(f"slave {slave} failed to set target position")
        time.sleep(0.02)
        if not client._write_controlword(slave, client.CMD_ENABLE_OPERATION):
            raise StageReciprocationError(f"slave {slave} failed to clear PP trigger bit")
        time.sleep(0.02)
        if not client._write_controlword(slave, client.CMD_ENABLE_OPERATION | 0x10):
            raise StageReciprocationError(f"slave {slave} failed to trigger PP move")

    def _finish_axis_pp(self, motor: MotorManager) -> None:
        client = motor.client
        slave = motor.slave
        if not client._restore_enabled_state(slave):
            raise StageReciprocationError(f"slave {slave} failed to restore enabled state")
        if not client.quick_stop(slave):
            raise StageReciprocationError(f"slave {slave} failed to quick stop after arrival")

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
                raise StageReciprocationError(f"cannot read current position for {axis} axis")
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

    def _validate_target_in_safe_range(self, target: Dict[str, Any], limits: Dict[str, Any], name: str) -> None:
        if not limits["enabled"]:
            return
        for axis in ("x", "y"):
            lo, hi = self._safe_bounds(limits, axis)
            value = int(target[axis])
            if value < lo or value > hi:
                raise StageReciprocationError(f"{name}.{axis}={value} is outside safe range [{lo}, {hi}]")

    def _validate_current_within_hard_limits(self, positions: Dict[str, int], cfg: Dict[str, Any]) -> None:
        limits = cfg["limits"]
        if not limits["enabled"]:
            return
        for axis, pos in positions.items():
            lo, hi = self._hard_bounds(limits, axis)
            if int(pos) < lo or int(pos) > hi:
                raise StageReciprocationError(f"{axis} current position {pos} is outside hard range [{lo}, {hi}]")


stage_reciprocation_controller = StageReciprocationController()
