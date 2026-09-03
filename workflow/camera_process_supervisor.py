"""Run every MVS camera call in a disposable child process.

The Hikrobot Python wrapper ultimately enters native code through ctypes.  A
Python thread cannot cancel a native call which has stopped returning.  This
module therefore keeps the SDK and every camera handle in a spawned process.
The API process only owns a small state machine and can terminate/recreate the
worker when a command misses its deadline.

The module deliberately has no import-time dependency on numpy, Pillow or the
MVS package.  That keeps API startup and status endpoints available even when
the camera runtime is broken or absent.
"""
from __future__ import annotations

import copy
import logging
import math
import multiprocessing
import os
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Mapping


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEVICES_DIR = PROJECT_ROOT / "devices"

def _env_seconds(name: str, default: float, *, minimum: float, maximum: float) -> float:
    """Load a bounded finite timeout and fail API startup with a useful error."""

    raw = os.getenv(name)
    try:
        value = float(default if raw is None else raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be a number of seconds, got {raw!r}") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise RuntimeError(
            f"{name} must be finite and between {minimum:g} and {maximum:g} seconds, got {value!r}"
        )
    return value


WORKER_STARTUP_TIMEOUT_S = _env_seconds(
    "COLONY_CAMERA_WORKER_STARTUP_TIMEOUT_S", 8.0, minimum=0.1, maximum=120.0
)
OPEN_COMMAND_TIMEOUT_S = _env_seconds(
    "COLONY_CAMERA_OPEN_TIMEOUT_S", 20.0, minimum=0.1, maximum=300.0
)
STOP_COMMAND_TIMEOUT_S = _env_seconds(
    "COLONY_CAMERA_STOP_TIMEOUT_S", 75.0, minimum=0.1, maximum=300.0
)
CLOSE_COMMAND_TIMEOUT_S = _env_seconds(
    "COLONY_CAMERA_CLOSE_TIMEOUT_S", 45.0, minimum=0.1, maximum=300.0
)
CAPTURE_COMMAND_SLACK_S = _env_seconds(
    "COLONY_CAMERA_CAPTURE_SLACK_S", 5.0, minimum=0.05, maximum=60.0
)
TERMINATE_GRACE_S = _env_seconds(
    "COLONY_CAMERA_TERMINATE_GRACE_S", 2.0, minimum=0.1, maximum=30.0
)
MONITOR_INTERVAL_S = _env_seconds(
    "COLONY_CAMERA_MONITOR_INTERVAL_S", 0.5, minimum=0.05, maximum=60.0
)
MONITOR_COMMAND_TIMEOUT_S = _env_seconds(
    "COLONY_CAMERA_MONITOR_TIMEOUT_S", 2.0, minimum=0.05, maximum=60.0
)
RECORDING_STALL_MIN_S = _env_seconds(
    "COLONY_CAMERA_RECORD_STALL_MIN_S", 20.0, minimum=1.0, maximum=3600.0
)
RECORDING_STALL_GRACE_S = _env_seconds(
    "COLONY_CAMERA_RECORD_STALL_GRACE_S", 5.0, minimum=0.1, maximum=300.0
)

ACTIVE_STATES = {"opening", "open", "starting", "recording", "stopping", "closing", "recovering"}

CAMERA_WORKER_LOG_PATH = PROJECT_ROOT / "logs" / "camera_worker.log"
CAMERA_WORKER_LOG_MAX_BYTES = 10 * 1024 * 1024
CAMERA_WORKER_LOG_BACKUP_COUNT = 5


class CameraProcessError(RuntimeError):
    """Base error raised by the process boundary."""


class CameraProcessTimeout(CameraProcessError):
    """A worker command missed its deadline and the worker was terminated."""


class CameraWorkerError(CameraProcessError):
    """The worker returned an SDK/controller error."""

    def __init__(self, message: str, *, error_type: str = "CameraWorkerError", remote_traceback: str = "") -> None:
        super().__init__(message)
        self.error_type = str(error_type)
        self.remote_traceback = str(remote_traceback)


class CameraStateError(CameraProcessError):
    """The requested operation is invalid for the current supervisor state."""


def _camera_error_code(error: BaseException | str) -> str:
    if isinstance(error, CameraWorkerError):
        return error.error_type
    if isinstance(error, BaseException):
        return type(error).__name__
    return "CameraProcessError"


@dataclass(frozen=True)
class RemoteFrameInfo:
    width: int
    height: int
    frame_num: int
    pixel_type: int
    frame_len: int
    saved_path: str
    timestamp: float

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RemoteFrameInfo":
        return cls(
            width=int(payload.get("width") or 0),
            height=int(payload.get("height") or 0),
            frame_num=int(payload.get("frame_num") or 0),
            pixel_type=int(payload.get("pixel_type") or 0),
            frame_len=int(payload.get("frame_len") or 0),
            saved_path=str(payload.get("saved_path") or ""),
            timestamp=float(payload.get("timestamp") or time.time()),
        )


@dataclass(frozen=True)
class RemoteVideoInfo:
    saved_path: str
    width: int
    height: int
    pixel_type: int
    frame_rate: float
    bitrate_kbps: int
    frame_count: int
    duration_s: float
    timestamp_started: float
    timestamp_finished: float

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RemoteVideoInfo":
        return cls(
            saved_path=str(payload.get("saved_path") or ""),
            width=int(payload.get("width") or 0),
            height=int(payload.get("height") or 0),
            pixel_type=int(payload.get("pixel_type") or 0),
            frame_rate=float(payload.get("frame_rate") or 0.0),
            bitrate_kbps=int(payload.get("bitrate_kbps") or 0),
            frame_count=int(payload.get("frame_count") or 0),
            duration_s=float(payload.get("duration_s") or 0.0),
            timestamp_started=float(payload.get("timestamp_started") or 0.0),
            timestamp_finished=float(payload.get("timestamp_finished") or 0.0),
        )


@dataclass(frozen=True)
class _RecordingOperationPhase:
    """Immutable value used for compare-and-set operation ownership."""

    name: str
    owner_token: str | None = None


@dataclass(frozen=True)
class _RecordingOperationContext:
    """Immutable identity and inputs for one start/stop command.

    ``done_event`` is intentionally created once and never replaced.  It lets
    shutdown wait for the exact command it observed instead of a mutable
    ``_operation_thread`` reference that may already point at another phase.
    """

    token: str
    kind: str
    operation_id: str
    session_id: str
    settings: Mapping[str, Any]
    command_payload: Mapping[str, Any]
    done_event: threading.Event


def _object_payload(value: Any, fields: tuple[str, ...]) -> Dict[str, Any]:
    return {name: getattr(value, name, None) for name in fields}


def _frame_payload(frame: Any) -> Dict[str, Any]:
    return _object_payload(
        frame,
        ("width", "height", "frame_num", "pixel_type", "frame_len", "saved_path", "timestamp"),
    )


def _video_payload(video: Any) -> Dict[str, Any]:
    return _object_payload(
        video,
        (
            "saved_path",
            "width",
            "height",
            "pixel_type",
            "frame_rate",
            "bitrate_kbps",
            "frame_count",
            "duration_s",
            "timestamp_started",
            "timestamp_finished",
        ),
    )


def _camera_settings_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    def normalized_pixel(value: Any) -> str:
        return str(value or "mono8").strip().lower().replace("-", "").replace("_", "")

    if normalized_pixel(left.get("pixel_format")) != normalized_pixel(right.get("pixel_format")):
        return False
    left_serial = str(left.get("serial_number") or "")
    right_serial = str(right.get("serial_number") or "")
    if left_serial or right_serial:
        identity_matches = left_serial == right_serial
    else:
        left_ip = str(left.get("camera_ip") or "")
        right_ip = str(right.get("camera_ip") or "")
        if left_ip or right_ip:
            identity_matches = left_ip == right_ip
        else:
            identity_matches = int(left.get("device_index") or 0) == int(
                right.get("device_index") or 0
            )
    if not identity_matches:
        return False

    requested_auto = right.get("exposure_auto")
    requested_exposure = right.get("exposure_us")

    def effective_exposure_auto(settings: Mapping[str, Any]) -> bool | None:
        configured = settings.get("exposure_auto")
        if configured is not None:
            return bool(configured)
        # A concrete exposure is how the existing recording API requests
        # manual mode; older callers did not carry exposure_auto explicitly.
        if settings.get("exposure_us") is not None:
            return False
        return None

    active_auto = effective_exposure_auto(left)
    requested_effective_auto = effective_exposure_auto(right)

    # ``None`` on the requesting side is a wildcard.  Explicit automatic and
    # manual modes may never share a session, and an explicit exposure value
    # itself requests manual mode even if exposure_auto was omitted.
    if requested_auto is not None:
        if active_auto is None or active_auto != requested_effective_auto:
            return False
        if bool(requested_auto) and requested_exposure is not None:
            return False
    elif requested_exposure is not None:
        if active_auto is None or active_auto is not False:
            return False

    def requested_number_matches(name: str) -> bool:
        requested = right.get(name)
        if requested is None:
            return True
        active = left.get(name)
        if active is None:
            return False
        try:
            requested_value = float(requested)
            active_value = float(active)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(requested_value) or not math.isfinite(active_value):
            return False
        return math.isclose(active_value, requested_value, rel_tol=1e-6, abs_tol=1e-6)

    return requested_number_matches("exposure_us") and requested_number_matches("gain")


def _build_controller(settings: Mapping[str, Any]):
    if str(DEVICES_DIR) not in sys.path:
        sys.path.insert(0, str(DEVICES_DIR))
    from mvs_runtime import ensure_mvs_native_libraries  # type: ignore
    from camera_controller import HikCameraController  # type: ignore

    ensure_mvs_native_libraries()

    return HikCameraController(
        mvs_python_dir=settings.get("mvs_python_dir"),
        device_index=int(settings.get("device_index") or 0),
        serial_number=settings.get("serial_number"),
        camera_ip=settings.get("camera_ip"),
        pixel_format=str(settings.get("pixel_format") or "mono8"),
        default_exposure_us=None if settings.get("exposure_auto") is True else settings.get("exposure_us"),
        default_gain=settings.get("gain"),
    )


def _worker_response(request_id: str, *, result: Any = None, error: BaseException | None = None, fatal: bool = False):
    if error is None:
        return {"id": request_id, "ok": True, "result": result}
    return {
        "id": request_id,
        "ok": False,
        "error_type": type(error).__name__,
        "message": str(error),
        "traceback": traceback.format_exc(limit=20),
        "fatal": bool(fatal),
    }


def _configure_camera_worker_logging() -> None:
    """Persist native-worker diagnostics separately from the API process log."""

    root_logger = logging.getLogger()
    level_name = str(os.getenv("COLONY_CAMERA_WORKER_LOG_LEVEL", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s pid=%(process)d %(name)s: %(message)s",
        )
    if root_logger.getEffectiveLevel() > level:
        root_logger.setLevel(level)

    raw_path = str(os.getenv("COLONY_CAMERA_WORKER_LOG_PATH", str(CAMERA_WORKER_LOG_PATH))).strip()
    if not raw_path:
        return
    log_path = Path(raw_path)
    if not log_path.is_absolute():
        log_path = PROJECT_ROOT / log_path
    resolved = str(log_path.resolve(strict=False))
    if any(
        getattr(handler, "_colony_camera_worker_log_path", None) == resolved
        for handler in root_logger.handlers
    ):
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            resolved,
            maxBytes=CAMERA_WORKER_LOG_MAX_BYTES,
            backupCount=CAMERA_WORKER_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setLevel(level)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s pid=%(process)d [%(name)s] %(message)s"
            )
        )
        setattr(handler, "_colony_camera_worker_log_path", resolved)
        root_logger.addHandler(handler)
    except Exception:
        logger.exception("failed to configure camera worker file logging path=%s", resolved)


def _camera_worker_main(command_connection) -> None:
    """Child process entry point.  Never call this function in the API process."""

    _configure_camera_worker_logging()
    if str(DEVICES_DIR) not in sys.path:
        sys.path.insert(0, str(DEVICES_DIR))
    try:
        from mvs_runtime import ensure_mvs_native_libraries  # type: ignore
        ensure_mvs_native_libraries()
    except Exception:
        logger.exception("camera worker could not preload MVS native libraries")
    logger.info("camera worker booting pid=%s parent_pid=%s", os.getpid(), os.getppid())
    cam = None
    session_id: str | None = None
    settings: Dict[str, Any] = {}
    should_exit = False
    try:
        command_connection.send({"kind": "ready", "pid": os.getpid()})
        while not should_exit:
            try:
                request = command_connection.recv()
            except EOFError:
                break
            request_id = str(request.get("id") or "")
            command = str(request.get("command") or "")
            payload = dict(request.get("payload") or {})
            response = None
            try:
                if command == "ping":
                    result = {"pid": os.getpid(), "status": "ready"}

                elif command == "open":
                    if cam is not None:
                        raise RuntimeError("camera worker already owns a camera session")
                    candidate = _build_controller(payload.get("settings") or {})
                    try:
                        candidate.open()
                        if settings_payload := payload.get("settings"):
                            if settings_payload.get("exposure_auto") is not None:
                                candidate.set_exposure_auto(bool(settings_payload.get("exposure_auto")))
                    except BaseException:
                        if not bool(getattr(candidate, "_sdk_hung", False)):
                            try:
                                candidate.close()
                            except BaseException:
                                pass
                        # Even a reported successful rollback cannot prove a
                        # partially-created native handle is reusable.  Taint
                        # the worker so the parent replaces the whole process.
                        setattr(candidate, "_sdk_hung", True)
                        cam = candidate
                        raise
                    cam = candidate
                    session_id = str(payload.get("session_id") or uuid.uuid4().hex)
                    settings = dict(payload.get("settings") or {})
                    result = {"session_id": session_id, "status": cam.recording_status()}

                elif command == "start_recording":
                    if cam is not None:
                        raise RuntimeError("camera worker already owns a camera session")
                    candidate = _build_controller(payload.get("settings") or {})
                    try:
                        candidate.open()
                        if settings_payload := payload.get("settings"):
                            if settings_payload.get("exposure_auto") is not None:
                                candidate.set_exposure_auto(bool(settings_payload.get("exposure_auto")))
                        candidate.start_background_recording(
                            save_path=str(payload["recording_path"]),
                            fps=payload.get("fps"),
                            bitrate_kbps=int(payload.get("bitrate_kbps") or 1000),
                            timeout_ms=payload.get("timeout_ms"),
                        )
                    except BaseException:
                        if not bool(getattr(candidate, "_sdk_hung", False)):
                            try:
                                candidate.close()
                            except BaseException:
                                pass
                        setattr(candidate, "_sdk_hung", True)
                        cam = candidate
                        raise
                    cam = candidate
                    session_id = str(payload.get("session_id") or uuid.uuid4().hex)
                    settings = dict(payload.get("settings") or {})
                    result = {"session_id": session_id, "status": cam.recording_status()}

                elif command == "capture":
                    if cam is None:
                        raise RuntimeError("camera worker has no open camera")
                    if str(payload.get("session_id") or "") != str(session_id or ""):
                        raise RuntimeError("camera session is stale")
                    frame = cam.capture_once(str(payload["save_path"]), timeout_ms=payload.get("timeout_ms"))
                    result = {"frame": _frame_payload(frame), "status": cam.recording_status()}

                elif command == "set_exposure":
                    if cam is None:
                        raise RuntimeError("camera worker has no open camera")
                    if str(payload.get("session_id") or "") != str(session_id or ""):
                        raise RuntimeError("camera session is stale")
                    value = float(payload["value"])
                    cam.set_exposure_us(value)
                    settings["exposure_auto"] = False
                    settings["exposure_us"] = value
                    result = {"value": value, "status": cam.recording_status()}

                elif command == "set_gain":
                    if cam is None:
                        raise RuntimeError("camera worker has no open camera")
                    if str(payload.get("session_id") or "") != str(session_id or ""):
                        raise RuntimeError("camera session is stale")
                    value = float(payload["value"])
                    cam.set_gain(value)
                    settings["gain"] = value
                    result = {"value": value, "status": cam.recording_status()}

                elif command == "record_video":
                    if cam is None:
                        raise RuntimeError("camera worker has no open camera")
                    if str(payload.get("session_id") or "") != str(session_id or ""):
                        raise RuntimeError("camera session is stale")
                    video = cam.record_video(
                        str(payload["recording_path"]),
                        duration_s=float(payload["duration_s"]),
                        fps=payload.get("fps"),
                        bitrate_kbps=int(payload.get("bitrate_kbps") or 1000),
                        timeout_ms=payload.get("timeout_ms"),
                    )
                    result = {"video": _video_payload(video), "status": cam.recording_status()}

                elif command == "stop_recording":
                    if cam is None:
                        raise RuntimeError("camera worker has no open camera")
                    if str(payload.get("session_id") or "") != str(session_id or ""):
                        raise RuntimeError("camera session is stale")
                    video = None
                    if bool(getattr(cam, "recording", False)) or bool(getattr(cam, "is_background_recording", False)):
                        video = cam.stop_background_recording(
                            join_timeout_s=float(payload.get("join_timeout_s") or 5.0)
                        )
                    closed = bool(cam.close())
                    if not closed:
                        raise RuntimeError("camera controller did not close cleanly")
                    result = {"video": _video_payload(video) if video is not None else {}}
                    cam = None
                    session_id = None
                    settings = {}

                elif command == "close":
                    if cam is None:
                        result = {"closed": True}
                    else:
                        if str(payload.get("session_id") or "") != str(session_id or ""):
                            raise RuntimeError("camera session is stale")
                        if bool(getattr(cam, "recording", False)):
                            raise RuntimeError("cannot close an active recording session")
                        try:
                            closed = bool(cam.close())
                        except BaseException:
                            # A failed close leaves native ownership uncertain.
                            # Mark the response fatal so the parent quarantines
                            # this entire process instead of reusing the handle.
                            setattr(cam, "_sdk_hung", True)
                            raise
                        if not closed:
                            setattr(cam, "_sdk_hung", True)
                            raise RuntimeError("camera controller did not close cleanly")
                        result = {"closed": True}
                        cam = None
                        session_id = None
                        settings = {}

                elif command == "status":
                    if cam is None:
                        result = {"opened": False, "recording": False, "background": False}
                    else:
                        result = dict(cam.recording_status())
                    result["session_id"] = session_id
                    result["settings"] = dict(settings)

                elif command == "shutdown":
                    close_error = None
                    video = None
                    if cam is not None:
                        try:
                            if bool(getattr(cam, "recording", False)) or bool(
                                getattr(cam, "is_background_recording", False)
                            ):
                                video = cam.stop_background_recording(
                                    join_timeout_s=float(payload.get("join_timeout_s") or 5.0)
                                )
                            if not bool(cam.close()):
                                raise RuntimeError("camera controller did not close cleanly")
                        except BaseException as exc:
                            close_error = exc
                    if close_error is not None:
                        raise close_error
                    # This generic path is also the last-resort shutdown path
                    # when the parent observed STARTING/STOPPING between the
                    # native response and its local state commit.  Returning
                    # metadata lets the parent validate and promote the part
                    # file instead of silently losing a completed recording.
                    result = {
                        "closed": True,
                        "video": _video_payload(video) if video is not None else {},
                    }
                    should_exit = True

                else:
                    raise ValueError(f"unknown camera worker command: {command}")

                response = _worker_response(request_id, result=result)
            except BaseException as exc:
                fatal = bool(cam is not None and getattr(cam, "_sdk_hung", False))
                response = _worker_response(request_id, error=exc, fatal=fatal)
                if fatal:
                    should_exit = True
            try:
                command_connection.send(response)
            except (BrokenPipeError, EOFError, OSError):
                break
    finally:
        # Do not attempt cleanup after the parent has broken the pipe.  The
        # process boundary is the cancellation mechanism and a second native
        # call here could hang process termination.
        try:
            command_connection.close()
        except Exception:
            pass


class CameraProcessProxy:
    """Small API-process proxy compatible with the existing capture call sites."""

    def __init__(
        self,
        supervisor: "CameraProcessSupervisor",
        session_id: str,
        settings: Mapping[str, Any],
        *,
        recording_shared: bool,
    ) -> None:
        self._supervisor = supervisor
        self.session_id = str(session_id)
        self.settings = dict(settings)
        self.recording_shared = bool(recording_shared)
        self._closed = False

    @property
    def opened(self) -> bool:
        return not self._closed and self._supervisor.session_is_usable(self.session_id)

    @property
    def recording(self) -> bool:
        status = self._supervisor.status()
        return bool(status.get("recording") and status.get("session_id") == self.session_id)

    @property
    def is_background_recording(self) -> bool:
        return self.recording

    def capture_once(self, save_path: str, timeout_ms: int | None = None) -> RemoteFrameInfo:
        if self._closed:
            raise CameraStateError("camera proxy is closed")
        return self._supervisor.capture(self.session_id, save_path, timeout_ms=timeout_ms)

    def capture_bmp(self, save_path: str, timeout_ms: int | None = None) -> RemoteFrameInfo:
        return self.capture_once(str(Path(save_path).with_suffix(".bmp")), timeout_ms=timeout_ms)

    def capture_jpg(self, save_path: str, timeout_ms: int | None = None) -> RemoteFrameInfo:
        return self.capture_once(str(Path(save_path).with_suffix(".jpg")), timeout_ms=timeout_ms)

    def capture_png(self, save_path: str, timeout_ms: int | None = None) -> RemoteFrameInfo:
        return self.capture_once(str(Path(save_path).with_suffix(".png")), timeout_ms=timeout_ms)

    def set_exposure_us(self, exposure_us: float) -> None:
        value = float(exposure_us)
        self._supervisor.set_exposure(self.session_id, value)
        self.settings["exposure_auto"] = False
        self.settings["exposure_us"] = value

    def set_gain(self, gain: float) -> None:
        value = float(gain)
        self._supervisor.set_gain(self.session_id, value)
        self.settings["gain"] = value

    def record_video(
        self,
        save_path: str,
        *,
        duration_s: float,
        fps: float | None = None,
        bitrate_kbps: int = 1000,
        timeout_ms: int | None = None,
    ) -> RemoteVideoInfo:
        return self._supervisor.record_video(
            self.session_id,
            save_path,
            duration_s=duration_s,
            fps=fps,
            bitrate_kbps=bitrate_kbps,
            timeout_ms=timeout_ms,
        )

    def close(self) -> bool:
        if self._closed:
            return True
        if self.recording_shared:
            self._closed = True
            return True
        closed = self._supervisor.close_camera(self.session_id)
        self._closed = bool(closed)
        return bool(closed)


class CameraProcessSupervisor:
    """Thread-safe process owner and authoritative camera state machine."""

    def __init__(
        self,
        *,
        worker_target: Callable[..., None] = _camera_worker_main,
        mp_context=None,
        monitor_interval_s: float = MONITOR_INTERVAL_S,
    ) -> None:
        self._context = mp_context or multiprocessing.get_context("spawn")
        self._worker_target = worker_target
        self._monitor_interval_s = max(float(monitor_interval_s), 0.05)
        self._state_lock = threading.RLock()
        self._command_lock = threading.Lock()
        self._process = None
        self._connection = None
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None

        self._state = "idle"
        self._operation_id: str | None = None
        self._session_id: str | None = None
        self._settings: Dict[str, Any] = {}
        self._latest_status: Dict[str, Any] = {}
        self._last_video: Dict[str, Any] = {}
        self._last_error: str | None = None
        self._last_error_code: str | None = None
        self._last_transition_at = time.time()
        self._restart_count = 0
        self._worker_pid: int | None = None
        self._shutdown_requested = False
        self._operation_thread: threading.Thread | None = None
        self._recording_operation: _RecordingOperationContext | None = None
        self._recording_operation_phase: _RecordingOperationPhase | None = None
        self._last_observed_frame_count = 0
        self._last_frame_progress_monotonic: float | None = None
        self._last_frame_progress_at: float | None = None

    def _transition(
        self,
        state: str,
        *,
        operation_id: str | None = None,
        session_id: str | None = None,
        settings: Mapping[str, Any] | None = None,
        error: BaseException | str | None = None,
        error_code: str | None = None,
    ) -> None:
        with self._state_lock:
            self._state = str(state)
            self._operation_id = operation_id
            self._session_id = session_id
            if settings is not None:
                self._settings = dict(settings)
            if error is not None:
                self._last_error = str(error)
                self._last_error_code = str(error_code or type(error).__name__)
            if state == "recording":
                self._last_observed_frame_count = int(self._latest_status.get("frame_count") or 0)
                self._last_frame_progress_monotonic = time.monotonic()
                self._last_frame_progress_at = time.time()
            self._last_transition_at = time.time()

    def _clear_session(self, *, keep_error: bool = True, state: str = "idle") -> None:
        with self._state_lock:
            self._state = str(state)
            self._operation_id = None
            self._session_id = None
            self._settings = {}
            self._latest_status = {}
            self._last_observed_frame_count = 0
            self._last_frame_progress_monotonic = None
            self._last_frame_progress_at = None
            if not keep_error:
                self._last_error = None
                self._last_error_code = None
            self._last_transition_at = time.time()

    def _operation_cas_locked(
        self,
        context: _RecordingOperationContext,
        expected: _RecordingOperationPhase,
        updated: _RecordingOperationPhase,
    ) -> bool:
        """Atomically replace an immutable operation phase.

        Callers must follow the process-wide lock order when a command is in
        flight: ``_command_lock`` first, then ``_state_lock``.  The context
        identity prevents a late thread from committing into a newer session.
        """

        if self._recording_operation is not context:
            return False
        if self._recording_operation_phase != expected:
            return False
        self._recording_operation_phase = updated
        return True

    def _finish_operation_locked(
        self,
        context: _RecordingOperationContext,
        expected: _RecordingOperationPhase,
    ) -> bool:
        if not self._operation_cas_locked(
            context,
            expected,
            _RecordingOperationPhase("completed", expected.owner_token),
        ):
            return False
        self._recording_operation = None
        self._recording_operation_phase = None
        context.done_event.set()
        return True

    def _cancel_operation_locked(
        self,
        context: _RecordingOperationContext,
        expected: _RecordingOperationPhase,
    ) -> bool:
        if not self._operation_cas_locked(
            context,
            expected,
            _RecordingOperationPhase("cancelled", expected.owner_token),
        ):
            return False
        self._recording_operation = None
        self._recording_operation_phase = None
        context.done_event.set()
        return True

    def _abandon_operation_locked(
        self,
        context: _RecordingOperationContext | None = None,
    ) -> bool:
        """Invalidate an in-flight recording command and wake its waiter.

        This is the last-resort path used after a worker is quarantined or the
        API shutdown budget is exhausted.  Callers must hold ``_state_lock``.
        Clearing the context before a late command thread resumes makes every
        subsequent compare-and-set fail, so it cannot resurrect stale state.
        """

        current = self._recording_operation
        if current is None or (context is not None and current is not context):
            return False
        phase = self._recording_operation_phase
        self._recording_operation_phase = _RecordingOperationPhase(
            "cancelled",
            phase.owner_token if phase is not None else None,
        )
        self._recording_operation = None
        self._recording_operation_phase = None
        current.done_event.set()
        return True

    @staticmethod
    def _immutable_operation_mapping(values: Mapping[str, Any]) -> Mapping[str, Any]:
        # Deep-copy before publishing the read-only top-level view.  API model
        # dumps contain a nested settings dict; a shallow copy would let a
        # caller mutate the exact payload sent to the worker after reservation.
        return MappingProxyType(copy.deepcopy(dict(values)))

    def _start_monitor(self) -> None:
        with self._state_lock:
            thread = self._monitor_thread
            if thread is not None and thread.is_alive():
                return
            self._monitor_stop.clear()
            thread = threading.Thread(target=self._monitor_loop, name="camera-worker-monitor", daemon=True)
            self._monitor_thread = thread
            thread.start()

    def _monitor_loop(self) -> None:
        while not self._monitor_stop.wait(self._monitor_interval_s):
            with self._state_lock:
                state = self._state
                shutting_down = self._shutdown_requested
            if shutting_down or state not in {"open", "recording"}:
                continue
            if not self._command_lock.acquire(blocking=False):
                continue
            try:
                try:
                    result = self._request_locked("status", {}, MONITOR_COMMAND_TIMEOUT_S)
                except CameraProcessError:
                    logger.exception("camera worker monitor detected a failed worker")
                    continue
                result_status = dict(result or {})
                reason: CameraWorkerError | None = None
                with self._state_lock:
                    # A stop/close request may reserve its transition while
                    # this fast status command owns the command lock.  Never
                    # apply the old state's response to the new state.
                    if self._state != state:
                        continue
                    if state == "recording" and (
                        not bool(result_status.get("recording"))
                        or not bool(result_status.get("background"))
                        or bool(result_status.get("sdk_hung"))
                        or bool(result_status.get("error"))
                    ):
                        reason = CameraWorkerError(
                            str(result_status.get("error") or "camera recording worker stopped unexpectedly"),
                            error_type="CAMERA_RECORDING_UNHEALTHY",
                        )
                    elif state == "recording":
                        frame_count = int(result_status.get("frame_count") or 0)
                        now_monotonic = time.monotonic()
                        if frame_count > self._last_observed_frame_count:
                            self._last_observed_frame_count = frame_count
                            self._last_frame_progress_monotonic = now_monotonic
                            self._last_frame_progress_at = time.time()
                        last_progress = self._last_frame_progress_monotonic or now_monotonic
                        fps = max(
                            float(
                                result_status.get("frame_rate")
                                or self._settings.get("fps")
                                or 10.0
                            ),
                            0.001,
                        )
                        grab_timeout_s = max(float(self._settings.get("timeout_ms") or 0) / 1000.0, 0.0)
                        stall_limit_s = max(
                            RECORDING_STALL_MIN_S,
                            grab_timeout_s + RECORDING_STALL_GRACE_S,
                            3.0 / fps,
                        )
                        if now_monotonic - last_progress > stall_limit_s:
                            reason = CameraWorkerError(
                                f"camera recording produced no new frame for "
                                f"{now_monotonic - last_progress:.1f}s "
                                f"(frame_count={frame_count}, limit={stall_limit_s:.1f}s)",
                                error_type="CAMERA_RECORDING_STALLED",
                            )
                    if reason is None:
                        self._latest_status = result_status
                if reason is not None:
                    self._discard_worker_locked(reason, restart=True)
                    logger.error("camera recording worker was quarantined after an unhealthy status")
                    continue
            finally:
                self._command_lock.release()

    def _spawn_worker_locked(self) -> None:
        process = self._process
        if process is not None:
            try:
                if process.is_alive() and self._connection is not None:
                    return
            except (OSError, ValueError):
                pass
        if self._shutdown_requested:
            raise CameraStateError("camera supervisor is shutting down")

        parent_connection = None
        child_connection = None
        process = None
        try:
            parent_connection, child_connection = self._context.Pipe(duplex=True)
            process = self._context.Process(
                target=self._worker_target,
                args=(child_connection,),
                name="colony-camera-worker",
                # Graceful shutdown is explicit below.  Daemon mode is the
                # final interpreter-exit safeguard so Python terminates a hung
                # native worker instead of joining it forever.
                daemon=True,
            )
            process.start()
            child_connection.close()
            child_connection = None
            if not parent_connection.poll(WORKER_STARTUP_TIMEOUT_S):
                raise CameraProcessTimeout(
                    f"camera worker did not start within {WORKER_STARTUP_TIMEOUT_S:.1f}s"
                )
            ready = parent_connection.recv()
        except CameraProcessError:
            self._terminate_process(process, parent_connection)
            raise
        except BaseException as exc:
            self._terminate_process(process, parent_connection)
            raise CameraProcessError(f"camera worker failed during startup: {exc}") from exc
        finally:
            if child_connection is not None:
                try:
                    child_connection.close()
                except Exception:
                    pass
        if not isinstance(ready, dict) or ready.get("kind") != "ready":
            self._terminate_process(process, parent_connection)
            raise CameraProcessError(f"camera worker returned invalid startup handshake: {ready!r}")
        self._process = process
        self._connection = parent_connection
        self._worker_pid = int(ready.get("pid") or process.pid or 0)
        self._start_monitor()
        logger.info("camera worker started pid=%s", self._worker_pid)

    @staticmethod
    def _signal_process_termination(process) -> None:
        """End a worker without closing/joining handles owned by another thread."""

        if process is None:
            return
        try:
            if process.is_alive():
                process.terminate()
        except Exception:
            logger.exception(
                "failed to signal camera worker termination pid=%s",
                getattr(process, "pid", None),
            )

    @staticmethod
    def _stop_process(process) -> None:
        if process is None:
            return
        try:
            process_pid = process.pid
        except (OSError, ValueError):
            process_pid = None
        try:
            if process.is_alive():
                process.terminate()
                process.join(timeout=TERMINATE_GRACE_S)
            if process.is_alive():
                kill = getattr(process, "kill", None)
                if callable(kill):
                    kill()
                else:
                    process.terminate()
                process.join(timeout=TERMINATE_GRACE_S)
        except Exception:
            logger.exception("failed to terminate camera worker pid=%s", process_pid)

    @classmethod
    def _terminate_process(cls, process, connection) -> None:
        try:
            connection.close()
        except Exception:
            pass
        cls._stop_process(process)
        if process is None:
            return
        try:
            process.close()
        except Exception:
            pass

    def _discard_worker_locked(self, reason: BaseException | str, *, restart: bool) -> None:
        process = self._process
        connection = self._connection
        old_pid = self._worker_pid
        self._process = None
        self._connection = None
        self._worker_pid = None
        self._transition("recovering", error=reason, error_code=_camera_error_code(reason))
        self._terminate_process(process, connection)
        self._restart_count += 1
        logger.error("camera worker pid=%s discarded: %s", old_pid, reason)

        if restart and not self._shutdown_requested:
            try:
                self._spawn_worker_locked()
            except Exception as exc:
                with self._state_lock:
                    self._clear_session(keep_error=True, state="faulted")
                    self._transition(
                        "faulted",
                        error=exc,
                        error_code="CAMERA_WORKER_RESTART_FAILED",
                    )
                    self._abandon_operation_locked()
                logger.exception("camera worker restart failed")
                return
            with self._state_lock:
                self._clear_session(keep_error=True)
                self._abandon_operation_locked()
        else:
            with self._state_lock:
                self._clear_session(keep_error=True)
                self._abandon_operation_locked()

    def _request_locked(self, command: str, payload: Mapping[str, Any], timeout_s: float) -> Any:
        if self._shutdown_requested and command not in {"shutdown", "stop_recording", "close"}:
            raise CameraStateError("camera supervisor is shutting down")

        existing_process = self._process
        if existing_process is not None:
            try:
                existing_alive = bool(existing_process.is_alive())
            except (OSError, ValueError):
                existing_alive = False
            if not existing_alive or self._connection is None:
                try:
                    exit_code = existing_process.exitcode
                except (OSError, ValueError):
                    exit_code = None
                exc = CameraProcessError(
                    f"camera worker was unavailable before {command}, exitcode={exit_code}"
                )
                with self._state_lock:
                    had_session = self._state != "idle" or self._session_id is not None
                self._discard_worker_locked(exc, restart=True)
                if had_session:
                    raise exc

        self._spawn_worker_locked()
        process = self._process
        connection = self._connection
        if process is None or connection is None:
            raise CameraProcessError("camera worker is unavailable")
        request_id = uuid.uuid4().hex
        try:
            connection.send({"id": request_id, "command": command, "payload": dict(payload)})
        except (BrokenPipeError, EOFError, OSError) as exc:
            wrapped = CameraProcessError(
                f"camera worker pipe failed while sending {command}: {type(exc).__name__}: {exc}"
            )
            self._discard_worker_locked(wrapped, restart=True)
            raise wrapped from exc

        deadline = time.monotonic() + max(float(timeout_s), 0.05)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                exc = CameraProcessTimeout(
                    f"camera worker command {command} timed out after {float(timeout_s):.1f}s"
                )
                self._discard_worker_locked(exc, restart=True)
                raise exc
            try:
                if not connection.poll(min(remaining, 0.1)):
                    if process is not None and not process.is_alive():
                        exc = CameraProcessError(
                            f"camera worker exited during {command}, exitcode={process.exitcode}"
                        )
                        self._discard_worker_locked(exc, restart=True)
                        raise exc
                    continue
                response = connection.recv()
            except (BrokenPipeError, EOFError, OSError, ValueError) as exc:
                wrapped = CameraProcessError(
                    f"camera worker pipe failed during {command}: {type(exc).__name__}: {exc}"
                )
                self._discard_worker_locked(wrapped, restart=True)
                raise wrapped from exc
            if not isinstance(response, dict) or str(response.get("id") or "") != request_id:
                exc = CameraProcessError(f"camera worker protocol mismatch during {command}: {response!r}")
                self._discard_worker_locked(exc, restart=True)
                raise exc
            if bool(response.get("ok")):
                return response.get("result")
            remote = CameraWorkerError(
                str(response.get("message") or f"camera worker {command} failed"),
                error_type=str(response.get("error_type") or "CameraWorkerError"),
                remote_traceback=str(response.get("traceback") or ""),
            )
            logger.error(
                "camera worker command=%s failed error_type=%s message=%s%s",
                command,
                remote.error_type,
                remote,
                f"\n{remote.remote_traceback}" if remote.remote_traceback else "",
            )
            if bool(response.get("fatal")):
                self._discard_worker_locked(remote, restart=True)
            raise remote

    def _request(self, command: str, payload: Mapping[str, Any], timeout_s: float) -> Any:
        with self._command_lock:
            return self._request_locked(command, payload, timeout_s)

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            state = self._state
            latest = dict(self._latest_status)
            settings = dict(self._settings)
            operation_id = self._operation_id
            session_id = self._session_id
            error = self._last_error
            error_code = self._last_error_code
            transitioned = self._last_transition_at
            worker_pid = self._worker_pid
            restarts = self._restart_count
            last_video = dict(self._last_video)
            last_frame_progress_at = self._last_frame_progress_at

        recording = bool(state == "recording" and latest.get("recording", True))
        background = bool(state == "recording" and latest.get("background", True))
        status = {
            "state": state,
            "recording": recording,
            "background": background,
            "starting": state == "starting",
            "stopping": state == "stopping",
            "opened": state in {"open", "recording", "stopping"},
            "sdk_hung": state in {"faulted", "recovering"},
            "operation_id": operation_id,
            "session_id": session_id,
            "worker_pid": worker_pid,
            "worker_restart_count": restarts,
            "last_transition_at": transitioned,
            "error": error,
            "error_code": error_code,
            "settings": settings,
            "last_video": last_video,
            "last_frame_progress_at": last_frame_progress_at,
        }
        for key in ("saved_path", "frame_rate", "bitrate_kbps", "frame_count", "duration_s"):
            if key in latest:
                status[key] = latest[key]
        if "saved_path" not in status and settings.get("save_path"):
            status["saved_path"] = settings.get("save_path")
        if "duration_s" not in status and state == "recording" and transitioned:
            status["duration_s"] = max(0.0, time.time() - transitioned)
        return status

    def is_busy(self) -> bool:
        with self._state_lock:
            return self._state in ACTIVE_STATES

    def is_recording_busy(self) -> bool:
        """Whether a recording lifecycle owns the camera hardware guard."""
        with self._state_lock:
            return self._state in {"starting", "recording", "stopping"}

    def session_is_usable(self, session_id: str) -> bool:
        with self._state_lock:
            return self._session_id == str(session_id) and self._state in {"open", "recording"}

    def open_camera(self, settings: Mapping[str, Any]) -> CameraProcessProxy:
        requested = dict(settings)
        with self._state_lock:
            if self._shutdown_requested:
                raise CameraStateError("camera supervisor is shutting down")
            if self._state == "recording" and _camera_settings_match(self._settings, requested):
                return CameraProcessProxy(
                    self,
                    str(self._session_id),
                    self._settings,
                    recording_shared=True,
                )
            if self._state not in {"idle", "faulted"}:
                raise CameraStateError(f"camera is busy: state={self._state}")
            self._last_error = None
            self._last_error_code = None
            operation_id = uuid.uuid4().hex
            session_id = uuid.uuid4().hex
            self._transition(
                "opening",
                operation_id=operation_id,
                session_id=session_id,
                settings=requested,
            )
        with self._command_lock:
            try:
                result = self._request_locked(
                    "open",
                    {"session_id": session_id, "settings": requested},
                    OPEN_COMMAND_TIMEOUT_S,
                )
            except BaseException as exc:
                with self._state_lock:
                    if self._operation_id == operation_id and self._state == "opening":
                        self._clear_session(keep_error=True)
                        self._last_error = str(exc)
                        self._last_error_code = _camera_error_code(exc)
                raise
            with self._state_lock:
                commit_allowed = (
                    not self._shutdown_requested
                    and self._state == "opening"
                    and self._operation_id == operation_id
                    and self._session_id == session_id
                )
            if not commit_allowed:
                cleanup_error: BaseException | None = None
                try:
                    self._request_locked(
                        "close",
                        {"session_id": session_id},
                        min(CLOSE_COMMAND_TIMEOUT_S, 5.0),
                    )
                except BaseException as exc:
                    cleanup_error = exc
                    if self._process is not None:
                        self._discard_worker_locked(exc, restart=False)
                with self._state_lock:
                    if self._operation_id == operation_id and self._session_id == session_id:
                        self._clear_session(keep_error=cleanup_error is None)
                    if cleanup_error is not None:
                        self._last_error = str(cleanup_error)
                        self._last_error_code = _camera_error_code(cleanup_error)
                raise CameraStateError("camera open completed after shutdown or was superseded")
            with self._state_lock:
                self._latest_status = dict((result or {}).get("status") or {})
                self._transition(
                    "open",
                    operation_id=operation_id,
                    session_id=session_id,
                    settings=requested,
                )
        return CameraProcessProxy(self, session_id, requested, recording_shared=False)

    def close_camera(self, session_id: str) -> bool:
        expected_session_id = str(session_id)
        with self._state_lock:
            if self._session_id != expected_session_id:
                return True
            if self._state == "recording":
                return True
            if self._state == "idle":
                return True
            if self._state != "open":
                raise CameraStateError(f"camera cannot close in state={self._state}")
            operation_id = self._operation_id or uuid.uuid4().hex
            self._transition(
                "closing",
                operation_id=operation_id,
                session_id=self._session_id,
                settings=self._settings,
            )
        try:
            self._request("close", {"session_id": session_id}, CLOSE_COMMAND_TIMEOUT_S)
        except BaseException as exc:
            with self._state_lock:
                if (
                    self._state == "closing"
                    and self._operation_id == operation_id
                    and self._session_id == expected_session_id
                ):
                    self._clear_session(keep_error=True)
                    self._last_error = str(exc)
                    self._last_error_code = _camera_error_code(exc)
            raise
        with self._state_lock:
            if (
                self._state == "closing"
                and self._operation_id == operation_id
                and self._session_id == expected_session_id
            ):
                self._clear_session(keep_error=False)
        return True

    def _reserve_recording_start(self, payload: Mapping[str, Any]) -> _RecordingOperationContext:
        command_payload = copy.deepcopy(dict(payload))
        settings = copy.deepcopy(dict(command_payload.get("settings") or {}))
        command_payload["settings"] = copy.deepcopy(settings)
        operation_id = uuid.uuid4().hex
        session_id = uuid.uuid4().hex
        command_payload["session_id"] = session_id
        context = _RecordingOperationContext(
            token=uuid.uuid4().hex,
            kind="start",
            operation_id=operation_id,
            session_id=session_id,
            settings=self._immutable_operation_mapping(settings),
            command_payload=self._immutable_operation_mapping(command_payload),
            done_event=threading.Event(),
        )
        with self._state_lock:
            if self._shutdown_requested:
                raise CameraStateError("camera supervisor is shutting down")
            if self._state not in {"idle", "faulted"}:
                raise CameraStateError(f"camera cannot start recording in state={self._state}")
            self._last_error = None
            self._last_error_code = None
            self._last_video = {}
            self._recording_operation = context
            self._recording_operation_phase = _RecordingOperationPhase("reserved")
            self._transition(
                "starting",
                operation_id=operation_id,
                session_id=session_id,
                settings=settings,
            )
        return context

    def _complete_recording_start(
        self,
        context: _RecordingOperationContext,
    ) -> Dict[str, Any]:
        owner_token = uuid.uuid4().hex
        reserved = _RecordingOperationPhase("reserved")
        requesting = _RecordingOperationPhase("requesting", owner_token)
        committing = _RecordingOperationPhase("committing", owner_token)
        with self._command_lock:
            with self._state_lock:
                if not self._operation_cas_locked(context, reserved, requesting):
                    raise CameraStateError("camera recording start was superseded")
            command_process = None
            try:
                self._spawn_worker_locked()
                command_process = self._process
                result = self._request_locked(
                    "start_recording",
                    dict(context.command_payload),
                    OPEN_COMMAND_TIMEOUT_S,
                )
            except BaseException as exc:
                if (
                    not isinstance(exc, CameraStateError)
                    and command_process is not None
                    and self._process is command_process
                ):
                    self._discard_worker_locked(exc, restart=True)
                with self._state_lock:
                    if self._recording_operation is context and self._recording_operation_phase == requesting:
                        if self._session_id == context.session_id:
                            self._clear_session(keep_error=True)
                        self._last_error = str(exc)
                        self._last_error_code = _camera_error_code(exc)
                        self._cancel_operation_locked(context, requesting)
                raise
            with self._state_lock:
                if (
                    self._state != "starting"
                    or self._operation_id != context.operation_id
                    or self._session_id != context.session_id
                    or not self._operation_cas_locked(context, requesting, committing)
                ):
                    raise CameraStateError("camera recording start response was superseded")
                self._latest_status = dict((result or {}).get("status") or {})
                self._transition(
                    "recording",
                    operation_id=context.operation_id,
                    session_id=context.session_id,
                    settings=context.settings,
                )
                if not self._finish_operation_locked(context, committing):
                    # This cannot happen while the state lock is held, but
                    # retaining the check makes the commit invariant explicit.
                    self._clear_session(keep_error=True)
                    raise CameraStateError("camera recording start commit lost ownership")
        return self.status()

    def start_recording(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        context = self._reserve_recording_start(payload)
        return self._complete_recording_start(context)

    def begin_start_recording(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Reserve STARTING synchronously and complete native startup in the background."""

        context = self._reserve_recording_start(payload)

        def run() -> None:
            try:
                self._complete_recording_start(context)
            except BaseException:
                logger.exception("asynchronous camera recording start failed")
            finally:
                with self._state_lock:
                    if self._operation_thread is threading.current_thread():
                        self._operation_thread = None

        thread = threading.Thread(target=run, name="camera-record-start", daemon=True)
        with self._state_lock:
            if (
                self._recording_operation is not context
                or self._recording_operation_phase != _RecordingOperationPhase("reserved")
            ):
                raise CameraStateError("camera recording start was cancelled before scheduling")
            self._operation_thread = thread
        try:
            thread.start()
        except BaseException as exc:
            with self._state_lock:
                if self._operation_thread is thread:
                    self._operation_thread = None
                reserved = _RecordingOperationPhase("reserved")
                if self._recording_operation is context and self._recording_operation_phase == reserved:
                    if self._session_id == context.session_id:
                        self._clear_session(keep_error=True)
                    self._last_error = str(exc)
                    self._last_error_code = _camera_error_code(exc)
                    self._cancel_operation_locked(context, reserved)
            raise
        return self.status()

    def _reserve_recording_stop(
        self,
        *,
        allow_during_shutdown: bool = False,
    ) -> _RecordingOperationContext:
        with self._state_lock:
            if self._shutdown_requested and not allow_during_shutdown:
                raise CameraStateError("camera supervisor is shutting down")
            if self._state == "stopping":
                raise CameraStateError("camera recording is already stopping")
            if self._state != "recording" or not self._session_id:
                raise CameraStateError("there is no active camera recording")
            operation_id = self._operation_id or uuid.uuid4().hex
            session_id = self._session_id
            settings = dict(self._settings)
            context = _RecordingOperationContext(
                token=uuid.uuid4().hex,
                kind="stop",
                operation_id=operation_id,
                session_id=session_id,
                settings=self._immutable_operation_mapping(settings),
                command_payload=self._immutable_operation_mapping({"session_id": session_id}),
                done_event=threading.Event(),
            )
            self._recording_operation = context
            self._recording_operation_phase = _RecordingOperationPhase("reserved")
            self._transition(
                "stopping",
                operation_id=operation_id,
                session_id=session_id,
                settings=settings,
            )
        return context

    def _finalize_recording_video(
        self, result: Mapping[str, Any] | None, settings: Mapping[str, Any]
    ) -> RemoteVideoInfo:
        video_payload = dict((result or {}).get("video") or {})
        if not video_payload:
            raise CameraWorkerError(
                "camera worker stopped without returning video metadata",
                error_type="CAMERA_RECORD_RESULT_MISSING",
            )
        info = RemoteVideoInfo.from_payload(video_payload)
        if not info.saved_path:
            raise CameraWorkerError(
                "camera worker returned an empty video path",
                error_type="CAMERA_RECORD_PATH_INVALID",
            )
        if info.frame_count <= 0:
            raise CameraWorkerError(
                "camera worker stopped without recording any frame",
                error_type="CAMERA_RECORD_EMPTY",
            )
        expected_part_value = settings.get("recording_path")
        if expected_part_value and Path(info.saved_path).resolve(strict=False) != Path(
            str(expected_part_value)
        ).resolve(strict=False):
            raise CameraWorkerError(
                f"camera worker returned an unexpected video path: {info.saved_path}",
                error_type="CAMERA_RECORD_PATH_MISMATCH",
            )
        part_path = Path(info.saved_path)
        try:
            part_size = part_path.stat().st_size
        except OSError as exc:
            raise CameraWorkerError(
                f"camera recording part file is unavailable: {part_path}: {exc}",
                error_type="CAMERA_RECORD_FILE_MISSING",
            ) from exc
        if part_size <= 0:
            raise CameraWorkerError(
                f"camera recording part file is empty: {part_path}",
                error_type="CAMERA_RECORD_FILE_EMPTY",
            )
        final_value = settings.get("save_path")
        if final_value:
            final_path = Path(str(final_value))
            if part_path.resolve(strict=False) != final_path.resolve(strict=False):
                final_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(part_path), str(final_path))
            info = RemoteVideoInfo(
                saved_path=str(final_path),
                width=info.width,
                height=info.height,
                pixel_type=info.pixel_type,
                frame_rate=info.frame_rate,
                bitrate_kbps=info.bitrate_kbps,
                frame_count=info.frame_count,
                duration_s=info.duration_s,
                timestamp_started=info.timestamp_started,
                timestamp_finished=info.timestamp_finished,
            )
        return info

    @staticmethod
    def _video_status_payload(info: RemoteVideoInfo) -> Dict[str, Any]:
        return {
            "saved_path": info.saved_path,
            "width": info.width,
            "height": info.height,
            "pixel_type": info.pixel_type,
            "frame_rate": info.frame_rate,
            "bitrate_kbps": info.bitrate_kbps,
            "frame_count": info.frame_count,
            "duration_s": info.duration_s,
            "timestamp_started": info.timestamp_started,
            "timestamp_finished": info.timestamp_finished,
        }

    def _complete_recording_stop(
        self,
        context: _RecordingOperationContext,
        *,
        join_timeout_s: float,
        command_timeout_s: float | None = None,
    ) -> RemoteVideoInfo:
        owner_token = uuid.uuid4().hex
        reserved = _RecordingOperationPhase("reserved")
        requesting = _RecordingOperationPhase("requesting", owner_token)
        finalizing = _RecordingOperationPhase("finalizing", owner_token)
        committing = _RecordingOperationPhase("committing", owner_token)

        with self._command_lock:
            with self._state_lock:
                if not self._operation_cas_locked(context, reserved, requesting):
                    if context.done_event.is_set() and self._last_video:
                        return RemoteVideoInfo.from_payload(self._last_video)
                    raise CameraStateError("camera recording stop was superseded")
            command_process = None
            try:
                self._spawn_worker_locked()
                command_process = self._process
                request_payload = dict(context.command_payload)
                request_payload["join_timeout_s"] = float(join_timeout_s)
                result = self._request_locked(
                    "stop_recording",
                    request_payload,
                    max(
                        float(
                            STOP_COMMAND_TIMEOUT_S
                            if command_timeout_s is None
                            else command_timeout_s
                        ),
                        0.05,
                    ),
                )
            except BaseException as exc:
                # Preserve the global lock order: command lock before state
                # lock.  A non-fatal native stop failure also leaves handle
                # ownership uncertain, so quarantine the worker here.
                if command_process is not None and self._process is command_process:
                    self._discard_worker_locked(exc, restart=True)
                with self._state_lock:
                    if self._recording_operation is context and self._recording_operation_phase == requesting:
                        if self._session_id == context.session_id:
                            self._clear_session(keep_error=True)
                        self._last_error = str(exc)
                        self._last_error_code = _camera_error_code(exc)
                        self._cancel_operation_locked(context, requesting)
                raise
            with self._state_lock:
                if (
                    self._state != "stopping"
                    or self._operation_id != context.operation_id
                    or self._session_id != context.session_id
                    or not self._operation_cas_locked(context, requesting, finalizing)
                ):
                    raise CameraStateError("camera recording stop response was superseded")

        try:
            info = self._finalize_recording_video(result, context.settings)
        except BaseException as exc:
            with self._state_lock:
                if self._recording_operation is context and self._recording_operation_phase == finalizing:
                    if self._session_id == context.session_id:
                        self._clear_session(keep_error=True)
                    self._last_error = str(exc)
                    self._last_error_code = (
                        exc.error_type
                        if isinstance(exc, CameraWorkerError)
                        else "CAMERA_RECORD_PROMOTE_FAILED"
                    )
                    self._cancel_operation_locked(context, finalizing)
            raise

        with self._state_lock:
            if (
                self._state != "stopping"
                or self._operation_id != context.operation_id
                or self._session_id != context.session_id
                or not self._operation_cas_locked(context, finalizing, committing)
            ):
                raise CameraStateError("camera recording finalization was superseded")
            self._last_video = self._video_status_payload(info)
            self._clear_session(keep_error=False)
            if not self._finish_operation_locked(context, committing):
                raise CameraStateError("camera recording stop commit lost ownership")
        return info

    def stop_recording(self, *, join_timeout_s: float = 5.0) -> RemoteVideoInfo:
        context = self._reserve_recording_stop()
        return self._complete_recording_stop(context, join_timeout_s=join_timeout_s)

    def begin_stop_recording(self, *, join_timeout_s: float = 5.0) -> Dict[str, Any]:
        """Reserve STOPPING synchronously and finalize/close in the background."""

        context = self._reserve_recording_stop()

        def run() -> None:
            try:
                self._complete_recording_stop(context, join_timeout_s=join_timeout_s)
            except BaseException:
                logger.exception("asynchronous camera recording stop failed")
            finally:
                with self._state_lock:
                    if self._operation_thread is threading.current_thread():
                        self._operation_thread = None

        thread = threading.Thread(target=run, name="camera-record-stop", daemon=True)
        with self._state_lock:
            if (
                self._recording_operation is not context
                or self._recording_operation_phase != _RecordingOperationPhase("reserved")
            ):
                raise CameraStateError("camera recording stop was cancelled before scheduling")
            self._operation_thread = thread
        try:
            thread.start()
        except BaseException as exc:
            with self._state_lock:
                if self._operation_thread is thread:
                    self._operation_thread = None
                reserved = _RecordingOperationPhase("reserved")
                if (
                    self._recording_operation is context
                    and self._recording_operation_phase == reserved
                    and self._state == "stopping"
                    and self._session_id == context.session_id
                ):
                    self._transition(
                        "recording",
                        operation_id=context.operation_id,
                        session_id=context.session_id,
                        settings=context.settings,
                        error=exc,
                        error_code=_camera_error_code(exc),
                    )
                    self._cancel_operation_locked(context, reserved)
            raise
        return self.status()

    def capture(self, session_id: str, save_path: str, *, timeout_ms: int | None = None) -> RemoteFrameInfo:
        with self._state_lock:
            if self._session_id != str(session_id) or self._state not in {"open", "recording"}:
                raise CameraStateError(f"camera session is not usable: state={self._state}")
        frame_timeout_ms = int(timeout_ms if timeout_ms is not None else 15_000)
        command_timeout = max(frame_timeout_ms / 1000.0 + CAPTURE_COMMAND_SLACK_S, CAPTURE_COMMAND_SLACK_S)
        result = self._request(
            "capture",
            {"session_id": session_id, "save_path": str(save_path), "timeout_ms": timeout_ms},
            command_timeout,
        )
        with self._state_lock:
            # A completed native response may race API lifespan shutdown after
            # ``_request`` releases the command lock.  Only the still-current
            # session may publish that response into parent-process state.
            if self._session_id == str(session_id) and self._state in {"open", "recording"}:
                self._latest_status = dict((result or {}).get("status") or self._latest_status)
        return RemoteFrameInfo.from_payload(dict((result or {}).get("frame") or {}))

    def set_exposure(self, session_id: str, value: float) -> None:
        if not self.session_is_usable(session_id):
            raise CameraStateError("camera session is not usable")
        exposure = float(value)
        self._request(
            "set_exposure",
            {"session_id": session_id, "value": exposure},
            OPEN_COMMAND_TIMEOUT_S,
        )
        with self._state_lock:
            if self._session_id == str(session_id) and self._state in {"open", "recording"}:
                self._settings["exposure_auto"] = False
                self._settings["exposure_us"] = exposure

    def set_gain(self, session_id: str, value: float) -> None:
        if not self.session_is_usable(session_id):
            raise CameraStateError("camera session is not usable")
        gain = float(value)
        self._request(
            "set_gain",
            {"session_id": session_id, "value": gain},
            OPEN_COMMAND_TIMEOUT_S,
        )
        with self._state_lock:
            if self._session_id == str(session_id) and self._state in {"open", "recording"}:
                self._settings["gain"] = gain

    def record_video(
        self,
        session_id: str,
        recording_path: str,
        *,
        duration_s: float,
        fps: float | None,
        bitrate_kbps: int,
        timeout_ms: int | None,
    ) -> RemoteVideoInfo:
        if not self.session_is_usable(session_id):
            raise CameraStateError("camera session is not usable")
        command_timeout = max(float(duration_s) + STOP_COMMAND_TIMEOUT_S, STOP_COMMAND_TIMEOUT_S)
        result = self._request(
            "record_video",
            {
                "session_id": session_id,
                "recording_path": str(recording_path),
                "duration_s": float(duration_s),
                "fps": fps,
                "bitrate_kbps": int(bitrate_kbps),
                "timeout_ms": timeout_ms,
            },
            command_timeout,
        )
        return RemoteVideoInfo.from_payload(dict((result or {}).get("video") or {}))

    def shutdown(self, *, timeout_s: float = 10.0) -> None:
        budget_s = max(float(timeout_s), 0.1)
        deadline = time.monotonic() + budget_s
        reserved_stop_context: _RecordingOperationContext | None = None
        with self._state_lock:
            self._shutdown_requested = True
            active_context = self._recording_operation
            active_phase = self._recording_operation_phase
            if (
                active_context is not None
                and active_context.kind == "start"
                and active_phase == _RecordingOperationPhase("reserved")
            ):
                # No native command owns the reservation yet.  Cancelling it
                # synchronously guarantees that a late scheduling thread fails
                # its CAS before it can create or reuse a worker.
                if self._session_id == active_context.session_id:
                    self._clear_session(keep_error=True)
                self._cancel_operation_locked(active_context, active_phase)
            elif (
                active_context is not None
                and active_context.kind == "stop"
                and active_phase == _RecordingOperationPhase("reserved")
            ):
                # Race the background thread for the same immutable context.
                # Exactly one owner can advance reserved -> requesting; the
                # loser observes the completed result instead of issuing a
                # duplicate native stop.
                reserved_stop_context = active_context
        self._monitor_stop.set()
        monitor_thread = self._monitor_thread
        if monitor_thread is not None and monitor_thread is not threading.current_thread():
            monitor_thread.join(timeout=min(1.0, max(deadline - time.monotonic(), 0.0)))

        if reserved_stop_context is not None:
            try:
                remaining = max(deadline - time.monotonic(), 0.1)
                self._complete_recording_stop(
                    reserved_stop_context,
                    join_timeout_s=min(max(remaining - 2.0, 0.1), 5.0),
                    command_timeout_s=remaining,
                )
            except CameraStateError:
                # Another owner won the CAS.  Its done_event is awaited below.
                pass
            except BaseException:
                logger.exception("reserved camera stop failed during API shutdown")

        with self._state_lock:
            active_context = self._recording_operation
            active_phase = self._recording_operation_phase
        if active_context is not None and not active_context.done_event.is_set():
            remaining = max(deadline - time.monotonic(), 0.0)
            if active_context.kind == "start":
                # Preserve time to stop a start command that completes while
                # shutdown is in progress.  With the production 30s budget,
                # startup gets up to 20s and graceful stop keeps at least 10s.
                stop_reserve = min(10.0, budget_s / 3.0)
                remaining = max(remaining - stop_reserve, 0.0)
            if remaining > 0.0:
                active_context.done_event.wait(remaining)

        if active_context is not None and not active_context.done_event.is_set():
            with self._state_lock:
                process = self._process
            logger.error(
                "camera %s operation phase=%s exceeded shutdown budget; terminating worker pid=%s",
                active_context.kind,
                active_phase.name if active_phase is not None else "unknown",
                getattr(process, "pid", None),
            )
            self._signal_process_termination(process)
            active_context.done_event.wait(min(TERMINATE_GRACE_S + 0.5, 3.0))

        with self._state_lock:
            needs_recording_stop = self._state == "recording" and bool(self._session_id)
        if needs_recording_stop:
            try:
                stop_context = self._reserve_recording_stop(allow_during_shutdown=True)
                remaining = max(deadline - time.monotonic(), 0.1)
                info = self._complete_recording_stop(
                    stop_context,
                    join_timeout_s=min(max(remaining - 2.0, 0.1), 5.0),
                    command_timeout_s=remaining,
                )
                logger.info(
                    "camera recording operation_id=%s finalized during API shutdown path=%s",
                    stop_context.operation_id,
                    info.saved_path,
                )
            except BaseException:
                logger.exception("camera recording did not finalize during API shutdown")

        acquired = self._command_lock.acquire(timeout=max(deadline - time.monotonic(), 0.0))
        if acquired:
            try:
                process = self._process
                connection = self._connection
                with self._state_lock:
                    shutdown_settings = dict(self._settings)
                    current_context = self._recording_operation
                    if current_context is not None:
                        shutdown_settings = dict(current_context.settings)
                try:
                    process_alive = bool(process is not None and process.is_alive())
                except (OSError, ValueError):
                    process_alive = False
                if process_alive and connection is not None:
                    try:
                        remaining = max(deadline - time.monotonic(), 0.1)
                        result = self._request_locked(
                            "shutdown",
                            {"join_timeout_s": min(max(remaining - 2.0, 0.1), 5.0)},
                            remaining,
                        )
                        if dict((result or {}).get("video") or {}):
                            info = self._finalize_recording_video(result, shutdown_settings)
                            with self._state_lock:
                                self._last_video = self._video_status_payload(info)
                        process.join(timeout=min(1.0, max(deadline - time.monotonic(), 0.0)))
                    except BaseException as exc:
                        with self._state_lock:
                            self._last_error = str(exc)
                            self._last_error_code = (
                                exc.error_type
                                if isinstance(exc, CameraWorkerError)
                                else _camera_error_code(exc)
                            )
                        logger.exception("camera worker did not shut down gracefully")
                remaining_process = self._process
                remaining_connection = self._connection
                self._process = None
                self._connection = None
                self._worker_pid = None
                self._terminate_process(
                    remaining_process if remaining_process is not None else process,
                    remaining_connection if remaining_connection is not None else connection,
                )
            finally:
                self._command_lock.release()
        else:
            # A long-running or hung command owns the command lock.  Signal
            # its process after the shutdown budget so API
            # lifespan cannot wait forever for a ctypes call.  Do not close
            # the Pipe while another thread is polling it on Windows: doing
            # so can leave an overlapped I/O object pending at interpreter
            # finalization.  Killing the child wakes the polling owner, which
            # then closes both handles while it still owns the command lock.
            with self._state_lock:
                process = self._process
            logger.error(
                "camera command did not finish within shutdown budget %.1fs; terminating worker pid=%s",
                budget_s,
                getattr(process, "pid", None),
            )
            self._signal_process_termination(process)
            cleanup_acquired = self._command_lock.acquire(
                timeout=max(2.0 * TERMINATE_GRACE_S + 1.0, 1.0)
            )
            if cleanup_acquired:
                try:
                    remaining_process = self._process
                    remaining_connection = self._connection
                    self._process = None
                    self._connection = None
                    self._worker_pid = None
                    self._terminate_process(remaining_process, remaining_connection)
                finally:
                    self._command_lock.release()
            else:
                logger.critical("camera command lock remained held after worker termination")

        operation_thread = self._operation_thread
        if (
            operation_thread is not None
            and operation_thread is not threading.current_thread()
            and operation_thread.ident is not None
        ):
            try:
                operation_thread.join(
                    timeout=max(
                        min(deadline - time.monotonic(), 1.0),
                        0.0,
                    )
                )
            except RuntimeError:
                # The narrow assignment -> Thread.start() window is allowed to
                # race shutdown.  The operation token was already cancelled;
                # a not-yet-started thread owns no resources to join.
                pass
        with self._state_lock:
            if self._operation_thread is operation_thread and (
                operation_thread is None or not operation_thread.is_alive()
            ):
                self._operation_thread = None
            unfinished_context = self._recording_operation
            if unfinished_context is not None:
                unfinished_phase = self._recording_operation_phase
                deferred_file_commit = bool(
                    operation_thread is not None
                    and operation_thread.is_alive()
                    and unfinished_context.kind == "stop"
                    and unfinished_phase is not None
                    and unfinished_phase.name in {"finalizing", "committing"}
                )
                if deferred_file_commit:
                    # Native ownership is already closed at this point.  Do
                    # not invalidate the exact token which is atomically
                    # promoting a local part file: when that bounded filesystem
                    # operation returns it must still commit last_video instead
                    # of leaving the final file and metadata inconsistent.
                    self._last_error = "camera video finalization continued past API shutdown budget"
                    self._last_error_code = "CAMERA_SHUTDOWN_FINALIZING"
                    logger.error(
                        "camera video finalization is still running after shutdown budget operation_id=%s",
                        unfinished_context.operation_id,
                    )
                    return
                if self._last_error is None:
                    self._last_error = "camera operation did not complete before API shutdown"
                    self._last_error_code = "CAMERA_SHUTDOWN_INCOMPLETE"
                self._abandon_operation_locked(unfinished_context)
            self._clear_session(keep_error=True)


_SUPERVISOR_LOCK = threading.Lock()
_SUPERVISOR: CameraProcessSupervisor | None = None
_SUPERVISOR_SHUTDOWN = False


def initialize_camera_process_supervisor() -> CameraProcessSupervisor:
    """Open the lifespan admission gate and create the local supervisor."""

    global _SUPERVISOR, _SUPERVISOR_SHUTDOWN
    with _SUPERVISOR_LOCK:
        _SUPERVISOR_SHUTDOWN = False
        if _SUPERVISOR is None:
            _SUPERVISOR = CameraProcessSupervisor()
        return _SUPERVISOR


def get_camera_process_supervisor() -> CameraProcessSupervisor:
    global _SUPERVISOR
    with _SUPERVISOR_LOCK:
        if _SUPERVISOR_SHUTDOWN:
            raise CameraStateError("camera supervisor is unavailable during API shutdown")
        if _SUPERVISOR is None:
            _SUPERVISOR = CameraProcessSupervisor()
        return _SUPERVISOR


def shutdown_camera_process_supervisor(*, timeout_s: float = 10.0) -> None:
    """Close admission, atomically detach the singleton, then stop it."""

    global _SUPERVISOR, _SUPERVISOR_SHUTDOWN
    with _SUPERVISOR_LOCK:
        _SUPERVISOR_SHUTDOWN = True
        supervisor = _SUPERVISOR
        _SUPERVISOR = None
    if supervisor is not None:
        supervisor.shutdown(timeout_s=timeout_s)


def reset_camera_process_supervisor_for_tests() -> None:
    """Dispose the singleton.  Intended only for deterministic tests."""

    global _SUPERVISOR_SHUTDOWN
    shutdown_camera_process_supervisor(timeout_s=1.0)
    with _SUPERVISOR_LOCK:
        _SUPERVISOR_SHUTDOWN = False
