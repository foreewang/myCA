"""管理位移台按配置进行后台往复运动的启动、停止和状态查询。"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict

from workflow.config_validator import validate_plates_file
from workflow.hardware_guard import require_hardware_safe
from workflow.plate_geometry import compute_well_start
from workflow.stage_executor import StageMotionError, move_to_absolute


class StageReciprocationError(RuntimeError):
    pass


class StageReciprocationController:
    """Run the stage through a fixed observation path in a background thread."""

    DEFAULT_PLATE_TYPE = "24-well"
    DEFAULT_SCAN_WELLS = ["B2", "B3", "B4", "C2", "C3", "C4"]
    DEFAULT_PLATES_PATH = Path(__file__).resolve().parent.parent / "config" / "plates.yaml"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_done = threading.Event()
        self._thread_done.set()
        self._shutdown_requested = False
        self._status: Dict[str, Any] = {
            "status": "stopped",
            "message": "not running",
        }

    def start(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        # Config loading may touch disk and safety admission takes the global
        # hardware lock. Keep both outside the controller lock so status/stop
        # calls remain responsive and the lock order cannot invert.
        require_hardware_safe()
        normalized = self._normalize_cfg(cfg)
        with self._lock:
            if self._shutdown_requested:
                raise StageReciprocationError("stage controller is shutting down")
            if self._thread is not None and self._thread.is_alive():
                raise StageReciprocationError("stage scan is already running")

            self._stop_event.clear()
            try:
                # Close the acquire -> latch -> start race.  If the latch was
                # engaged just before clear(), restore cancellation and refuse
                # to create the motion thread.  A later latch calls
                # request_stop(), whose Event.set() never waits on this lock.
                require_hardware_safe()
                if self._shutdown_requested:
                    raise StageReciprocationError("stage controller is shutting down")
            except BaseException:
                self._stop_event.set()
                raise
            self._thread_done.clear()
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
                daemon=False,
            )
            try:
                self._thread.start()
            except BaseException:
                self._thread = None
                self._thread_done.set()
                raise
            return dict(self._status)

    def request_stop(self) -> None:
        """Publish a non-blocking stop request, including during start races."""

        self._stop_event.set()

    def request_shutdown(self) -> None:
        """Close local admission and signal motion without joining its thread."""

        # These assignments intentionally precede any controller lock.  The
        # motion loop must observe shutdown even if start/status is stalled
        # while holding that lock.
        self._shutdown_requested = True
        self._stop_event.set()

    def stop(self, join_timeout_s: float = 5.0) -> Dict[str, Any]:
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._thread_done.set()
                if self._status.get("status") == "failed":
                    # A failed quick-stop or motion must remain visible.  The
                    # operator-facing stop endpoint is not evidence that the
                    # hardware reached a safe stopped state.
                    return dict(self._status)
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
                    "message": "stop requested; waiting for stage motion to stop",
                }
            return dict(self._status)

    def is_quiescent(self) -> bool:
        with self._lock:
            thread = self._thread
            return bool(self._thread_done.is_set() and (thread is None or not thread.is_alive()))

    def wait_until_quiescent(self, timeout_s: float | None = None) -> bool:
        timeout = None if timeout_s is None else max(0.0, float(timeout_s))
        if not self._thread_done.wait(timeout=timeout):
            return False
        return self.is_quiescent()

    def initialize(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise StageReciprocationError("previous stage scan is still shutting down")
            self._shutdown_requested = False

    def shutdown(self, join_timeout_s: float = 5.0) -> bool:
        self.request_shutdown()
        self.stop(join_timeout_s=join_timeout_s)
        return self.is_quiescent()

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
        targets, limits = self._load_scan_config(cfg)
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

    def _load_scan_config(self, cfg: Dict[str, Any]) -> tuple[list[Dict[str, Any]], Dict[str, Any]]:
        plates_path = Path(str(cfg.get("plates_path") or self.DEFAULT_PLATES_PATH))
        try:
            plates_cfg = validate_plates_file(plates_path)
            plate = plates_cfg["plates"][self.DEFAULT_PLATE_TYPE]
            limits = dict(plate["stage_limits"])
            if not limits["enabled"]:
                raise StageReciprocationError(
                    f"stage limits are disabled for plate config: {self.DEFAULT_PLATE_TYPE}"
                )

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
        except StageReciprocationError:
            raise
        except Exception as exc:
            raise StageReciprocationError(f"invalid plates config {plates_path}: {exc}") from exc

        return targets, limits

    def _run(self, cfg: Dict[str, Any]) -> None:
        try:
            self._run_impl(cfg)
        finally:
            self._thread_done.set()

    def _run_impl(self, cfg: Dict[str, Any]) -> None:
        cycles = 0
        target_index = 0
        targets = list(cfg["targets"])
        if not targets:
            raise StageReciprocationError("scan target list is empty")

        try:
            require_hardware_safe()
            with self._lock:
                self._status = {
                    **self._status,
                    "status": "running",
                    "message": "24-well stage scan running",
                    "cycle": cycles,
                    "target_count": len(targets),
                    "completed_targets": 0,
                }

            while not self._stop_event.is_set():
                require_hardware_safe()
                target = targets[target_index]
                move = self._move_target(target, cfg, cycles)
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

            with self._lock:
                current_pos = self._current_pos_from_move(self._status.get("last_move"))
                if current_pos is None:
                    current_pos = self._status.get("current_pos")
                self._status = {
                    **self._status,
                    "status": "stopped",
                    "message": "stage scan stopped",
                    "cycle": cycles,
                    "completed_targets": cycles * len(targets) + target_index,
                    "current_pos": current_pos,
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

    def _move_target(self, target: Dict[str, Any], cfg: Dict[str, Any], cycle: int) -> Dict[str, Any]:
        self._validate_target_in_safe_range(target, cfg["limits"], str(target.get("well_name") or "target"))
        with self._lock:
            self._status = {
                **self._status,
                "status": "moving",
                "message": "moving stage to scan well",
                "cycle": cycle,
                "target": dict(target),
            }

        try:
            move = move_to_absolute(
                port=cfg["port"],
                x_target=int(target["x"]),
                y_target=int(target["y"]),
                profile_vel=int(cfg["profile_vel"]),
                profile_acc=int(cfg["profile_acc"]),
                profile_dec=int(cfg["profile_dec"]),
                x_slave=int(cfg["x_slave"]),
                y_slave=int(cfg["y_slave"]),
                baudrate=int(cfg["baudrate"]),
                settle_s=float(cfg["settle_s"]),
                timeout_s=float(cfg["move_timeout_s"]),
                poll_s=float(cfg["poll_s"]),
                arrival_tolerance_pulse=int(cfg["arrival_tolerance"]),
                stage_limits=cfg["limits"],
                stop_event=self._stop_event,
                progress_callback=self._update_current_pos,
            )
        except StageMotionError as exc:
            raise StageReciprocationError(str(exc)) from exc

        move = dict(move)
        move["target"] = dict(target)
        with self._lock:
            self._status = {
                **self._status,
                "last_move": move,
                "current_pos": self._current_pos_from_move(move),
            }
        return move

    def _update_current_pos(self, current_pos: Dict[str, int]) -> None:
        with self._lock:
            self._status = {
                **self._status,
                "current_pos": dict(current_pos),
            }

    def _current_pos_from_move(self, move: Any) -> Dict[str, int] | None:
        if not isinstance(move, dict):
            return None
        after = move.get("after")
        if not isinstance(after, dict):
            return None
        try:
            return {
                "x": int(after["x"]["current_pos"]),
                "y": int(after["y"]["current_pos"]),
            }
        except Exception:
            return None

    def _safe_bounds(self, limits: Dict[str, Any], axis: str) -> tuple[int, int]:
        margin = int(limits["safety_margin"])
        return int(limits[f"{axis}_min"]) + margin, int(limits[f"{axis}_max"]) - margin

    def _validate_target_in_safe_range(self, target: Dict[str, Any], limits: Dict[str, Any], name: str) -> None:
        if not limits["enabled"]:
            return
        for axis in ("x", "y"):
            lo, hi = self._safe_bounds(limits, axis)
            value = int(target[axis])
            if value < lo or value > hi:
                raise StageReciprocationError(f"{name}.{axis}={value} is outside safe range [{lo}, {hi}]")


stage_reciprocation_controller = StageReciprocationController()


def initialize_stage_reciprocation_controller() -> None:
    stage_reciprocation_controller.initialize()


def shutdown_stage_reciprocation_controller(join_timeout_s: float = 5.0) -> bool:
    return stage_reciprocation_controller.shutdown(join_timeout_s=join_timeout_s)


def request_stage_reciprocation_shutdown() -> None:
    stage_reciprocation_controller.request_shutdown()


def stage_reciprocation_is_quiescent() -> bool:
    return stage_reciprocation_controller.is_quiescent()


def wait_for_stage_reciprocation(timeout_s: float | None = None) -> bool:
    return stage_reciprocation_controller.wait_until_quiescent(timeout_s=timeout_s)
