from __future__ import annotations

import textwrap

import pytest

from workflow.config_validator import (
    ConfigValidationError,
    resolve_mvs_python_dir,
    validate_autofocus_config,
    validate_autofocus_file,
    validate_camera_config,
    validate_camera_file,
    validate_handoff_config,
    validate_handoff_file,
    validate_plates_config,
    validate_plates_file,
)
from workflow.detect_api import normalize_detect_result
from workflow.handoff_executor import HandoffError, _check_arrival_tolerance
from workflow.plate_geometry import compute_well_start, parse_well_name, well_name_from_index
from workflow.stage_reciprocation import StageReciprocationController, StageReciprocationError


def test_well_name_round_trip_supports_multi_letter_rows() -> None:
    assert parse_well_name("A1") == (0, 0)
    assert parse_well_name("C6") == (2, 5)
    assert parse_well_name("AA12") == (26, 11)

    assert well_name_from_index(0, 0) == "A1"
    assert well_name_from_index(2, 5) == "C6"
    assert well_name_from_index(26, 11) == "AA12"


def test_compute_well_start_uses_plate_pitch_and_axis_signs() -> None:
    plate_cfg = {
        "rows": 4,
        "cols": 6,
        "a1_start": {"x": 8865800, "y": 6185500},
        "well_diameter_mm": 13.7,
        "well_gap_mm": 3.5,
        "pulses_per_mm": 147500,
        "row_stage_sign": -1,
        "col_stage_sign": -1,
    }

    assert compute_well_start(plate_cfg, "A1") == {
        "x": 8865800,
        "y": 6185500,
        "row_index": 0,
        "col_index": 0,
        "well_name": "A1",
    }
    assert compute_well_start(plate_cfg, "B2") == {
        "x": 6328800,
        "y": 3648500,
        "row_index": 1,
        "col_index": 1,
        "well_name": "B2",
    }


def test_normalize_detect_result_preserves_pickability_fields() -> None:
    raw = {
        "component_count": 2,
        "components": [
            {
                "id": "clone-a",
                "safe_point": [12.4, 34.6],
                "bbox": [1, 2, 3, 4],
                "area_px": 120,
                "score": 0.8,
                "is_valid_for_compensation": "true",
                "touch_image_border": 0,
                "image_border_sides": ["left"],
                "well_border_detected": True,
                "near_well_border": False,
                "distance_to_well_edge_px": 42.5,
                "distance_to_well_edge_mm": 0.85,
                "is_pickable": "yes",
            },
            {"id": "missing-center"},
        ],
    }

    result = normalize_detect_result(raw)

    assert result["clone_count"] == 2
    assert len(result["clones"]) == 1
    clone = result["clones"][0]
    assert clone["clone_id"] == "clone-a"
    assert clone["center_px"] == [12, 35]
    assert clone["bbox"] == [1, 2, 3, 4]
    assert clone["is_valid_for_compensation"] is True
    assert clone["touch_image_border"] is False
    assert clone["image_border_sides"] == ["left"]
    assert clone["well_border_detected"] is True
    assert clone["near_well_border"] is False
    assert clone["distance_to_well_edge_px"] == 42.5
    assert clone["distance_to_well_edge_mm"] == 0.85
    assert clone["is_pickable"] is True


def test_stage_reciprocation_normalize_cfg_builds_fixed_24_well_targets(tmp_path) -> None:
    plates_path = tmp_path / "plates.yaml"
    plates_path.write_text(
        textwrap.dedent(
            """
            plates:
              24-well:
                rows: 4
                cols: 6
                a1_start:
                  x: 8865800
                  y: 6185500
                well_diameter_mm: 13.7
                well_gap_mm: 3.5
                pulses_per_mm: 147500
                row_stage_sign: -1
                col_stage_sign: -1
            """
        ).strip(),
        encoding="utf-8",
    )

    cfg = StageReciprocationController()._normalize_cfg(
        {
            "plates_path": str(plates_path),
            "limit_check_enabled": True,
            "max_cycles": 3,
        }
    )

    assert cfg["plate_type"] == "24-well"
    assert cfg["scan_wells"] == ["B2", "B3", "B4", "C2", "C3", "C4"]
    assert cfg["max_cycles"] == 3
    assert cfg["targets"][0] == {
        "index": 1,
        "well_name": "B2",
        "x": 6328800,
        "y": 3648500,
    }
    assert cfg["targets"][-1]["well_name"] == "C4"


def test_stage_reciprocation_rejects_invalid_limits() -> None:
    controller = StageReciprocationController()
    with pytest.raises(StageReciprocationError, match="min must be smaller"):
        controller._normalize_cfg(
            {
                "limit_check_enabled": True,
                "x_min": 10,
                "x_max": 10,
            }
        )


def test_project_plates_config_passes_machine_validation() -> None:
    validate_plates_file("config/plates.yaml")


def test_project_camera_config_passes_machine_validation() -> None:
    validate_camera_file("config/camera.yaml")


def test_camera_controller_accepts_only_mono8_pixel_format() -> None:
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")

    from devices.camera_controller import CameraSDKError, HikCameraController

    assert HikCameraController._normalize_pixel_format("Mono_8") == "mono8"
    assert HikCameraController(pixel_format="monochrome8").pixel_format == "mono8"

    with pytest.raises(CameraSDKError, match="only mono8 is supported"):
        HikCameraController(pixel_format="rgb8")


def test_camera_controller_open_cleans_partial_resources_on_setup_failure() -> None:
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")

    import ctypes

    import devices.camera_controller as camera_controller
    from devices.camera_controller import CameraSDKError, HikCameraController

    camera_controller._MVS_SDK_REFCOUNT = 0
    camera_controller._MVS_SDK_INITIALIZED = False
    calls: list[str] = []

    class FakeMvCamera:
        @staticmethod
        def MV_CC_Initialize() -> int:
            calls.append("Initialize")
            return 0

        @staticmethod
        def MV_CC_Finalize() -> int:
            calls.append("Finalize")
            return 0

        def MV_CC_CreateHandle(self, *_args) -> int:
            calls.append("CreateHandle")
            return 0

        def MV_CC_OpenDevice(self, *_args) -> int:
            calls.append("OpenDevice")
            return 0

        def MV_CC_CloseDevice(self) -> int:
            calls.append("CloseDevice")
            return 0

        def MV_CC_DestroyHandle(self) -> int:
            calls.append("DestroyHandle")
            return 0

    class FailingOpenController(HikCameraController):
        def _load_sdk(self) -> None:
            self._sdk_loaded = True
            self._sdk = {"MvCamera": FakeMvCamera, "MV_ACCESS_Exclusive": 1}

        def _enum_devices(self):
            return object()

        def _select_device(self, _dev_list):
            return ctypes.c_int(1)

        def _try_set_optimal_packet_size(self) -> None:
            calls.append("PacketSize")

        def _set_pixel_format(self) -> None:
            calls.append("PixelFormat")

        def _set_trigger_mode(self) -> None:
            calls.append("TriggerMode")
            raise CameraSDKError("trigger setup failed")

    cam = FailingOpenController()

    with pytest.raises(CameraSDKError, match="trigger setup failed"):
        cam.open()

    assert calls == [
        "Initialize",
        "CreateHandle",
        "OpenDevice",
        "PacketSize",
        "PixelFormat",
        "TriggerMode",
        "CloseDevice",
        "DestroyHandle",
        "Finalize",
    ]
    assert cam.cam is None
    assert cam.device_info is None
    assert cam.opened is False
    assert cam.grabbing is False
    assert cam._sdk_initialized is False
    assert camera_controller._MVS_SDK_REFCOUNT == 0
    assert camera_controller._MVS_SDK_INITIALIZED is False


def test_camera_controller_uses_process_level_mvs_lifecycle_refcount() -> None:
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")

    import ctypes

    import devices.camera_controller as camera_controller
    from devices.camera_controller import HikCameraController

    camera_controller._MVS_SDK_REFCOUNT = 0
    camera_controller._MVS_SDK_INITIALIZED = False
    calls: list[str] = []

    class FakeMvCamera:
        @staticmethod
        def MV_CC_Initialize() -> int:
            calls.append("Initialize")
            return 0

        @staticmethod
        def MV_CC_Finalize() -> int:
            calls.append("Finalize")
            return 0

        def MV_CC_CreateHandle(self, *_args) -> int:
            calls.append("CreateHandle")
            return 0

        def MV_CC_OpenDevice(self, *_args) -> int:
            calls.append("OpenDevice")
            return 0

        def MV_CC_CloseDevice(self) -> int:
            calls.append("CloseDevice")
            return 0

        def MV_CC_DestroyHandle(self) -> int:
            calls.append("DestroyHandle")
            return 0

    class SuccessfulOpenController(HikCameraController):
        def _load_sdk(self) -> None:
            self._sdk_loaded = True
            self._sdk = {"MvCamera": FakeMvCamera, "MV_ACCESS_Exclusive": 1}

        def _enum_devices(self):
            return object()

        def _select_device(self, _dev_list):
            return ctypes.c_int(1)

        def _try_set_optimal_packet_size(self) -> None:
            calls.append("PacketSize")

        def _set_pixel_format(self) -> None:
            calls.append("PixelFormat")

        def _set_trigger_mode(self) -> None:
            calls.append("TriggerMode")

        def _start_grabbing_unlocked(self) -> None:
            calls.append("StartGrabbing")

        def _get_int_value(self, key: str) -> int:
            calls.append(key)
            return 1024

    cam1 = SuccessfulOpenController()
    cam2 = SuccessfulOpenController()

    try:
        cam1.open()
        cam2.open()

        assert calls.count("Initialize") == 1
        assert camera_controller._MVS_SDK_REFCOUNT == 2
        assert camera_controller._MVS_SDK_INITIALIZED is True

        cam1.close()
        assert calls.count("Finalize") == 0
        assert camera_controller._MVS_SDK_REFCOUNT == 1
        assert camera_controller._MVS_SDK_INITIALIZED is True

        cam2.close()
        assert calls.count("Finalize") == 1
        assert camera_controller._MVS_SDK_REFCOUNT == 0
        assert camera_controller._MVS_SDK_INITIALIZED is False
    finally:
        camera_controller._MVS_SDK_REFCOUNT = 0
        camera_controller._MVS_SDK_INITIALIZED = False


def test_camera_controller_refuses_to_stop_recording_while_worker_is_alive() -> None:
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")

    from devices.camera_controller import CameraSDKError, HikCameraController

    class StuckThread:
        def __init__(self) -> None:
            self.join_called = False

        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float | None = None) -> None:
            self.join_called = True

    thread = StuckThread()
    request = {"done": False, "error": None}
    cam = HikCameraController()
    cam.recording = True
    cam._record_thread = thread  # type: ignore[assignment]
    cam._snapshot_requests.append(request)

    with pytest.raises(CameraSDKError, match="后台录像线程未"):
        cam.stop_background_recording(join_timeout_s=0.01)

    assert thread.join_called is True
    assert cam.recording is True
    assert cam._record_thread is thread
    assert cam._record_error is not None
    assert request["done"] is True
    assert request["error"] == cam._record_error

    cam.close()
    assert cam.recording is True
    assert cam._record_thread is thread


def test_camera_controller_rejects_snapshot_when_background_thread_already_stopped(tmp_path) -> None:
    from devices.camera_controller import CameraSDKError, HikCameraController

    class StoppedThread:
        def is_alive(self) -> bool:
            return False

    cam = HikCameraController()
    cam.recording = True
    cam._record_thread = StoppedThread()  # type: ignore[assignment]

    with pytest.raises(CameraSDKError, match="background recording is not running"):
        cam.capture_snapshot_during_recording(str(tmp_path / "snapshot.bmp"), timeout_ms=1)

    assert cam._snapshot_requests == []


def test_camera_controller_recording_worker_fails_pending_snapshots_on_exit() -> None:
    from devices.camera_controller import HikCameraController

    request = {"done": False, "error": None}
    cam = HikCameraController()
    cam._snapshot_requests.append(request)
    cam._record_stop_event.set()

    cam._recording_worker(timeout_ms=1)

    assert request["done"] is True
    assert "background recording stopped" in request["error"]
    assert cam._snapshot_requests == []


def test_camera_controller_record_frame_rejects_non_mono8_before_input() -> None:
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")

    import ctypes

    from devices.camera_controller import CameraSDKError, HikCameraController

    class FakeFrameInfo(ctypes.Structure):
        _fields_ = [
            ("nWidth", ctypes.c_uint),
            ("nHeight", ctypes.c_uint),
            ("nFrameLen", ctypes.c_uint),
            ("enPixelType", ctypes.c_uint),
            ("nFrameNum", ctypes.c_uint),
        ]

    class FakeCam:
        def MV_CC_GetOneFrameTimeout(self, *args):
            return 0

    class BadPixelRecordingController(HikCameraController):
        def __init__(self) -> None:
            super().__init__()
            self.input_calls = 0
            self._sdk = {
                "MV_FRAME_OUT_INFO_EX": FakeFrameInfo,
                "PixelType_Gvsp_Mono8": 0x01080001,
            }
            self.cam = FakeCam()
            self.opened = True
            self.grabbing = True
            self.recording = True
            self.payload_size = 4

        def _set_command(self, key: str) -> None:
            return None

        def _call_variants(self, func, variants, func_name: str):
            frame_info = variants[0][2]
            frame_info.nWidth = 2
            frame_info.nHeight = 2
            frame_info.nFrameLen = 4
            frame_info.enPixelType = 999
            frame_info.nFrameNum = 17
            return 0

        def _input_record_frame(self, data_buf, frame_len: int) -> None:
            self.input_calls += 1

    cam = BadPixelRecordingController()

    with pytest.raises(CameraSDKError, match="record frame PixelFormat mismatch"):
        cam._record_one_frame_unlocked(timeout_ms=1)

    assert cam.input_calls == 0


def test_camera_controller_record_frame_accepts_valid_mono8_before_input() -> None:
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")

    import ctypes

    from devices.camera_controller import HikCameraController

    class FakeFrameInfo(ctypes.Structure):
        _fields_ = [
            ("nWidth", ctypes.c_uint),
            ("nHeight", ctypes.c_uint),
            ("nFrameLen", ctypes.c_uint),
            ("enPixelType", ctypes.c_uint),
            ("nFrameNum", ctypes.c_uint),
        ]

    class FakeCam:
        def MV_CC_GetOneFrameTimeout(self, *args):
            return 0

    class GoodPixelRecordingController(HikCameraController):
        def __init__(self) -> None:
            super().__init__()
            self.input_frame_len: int | None = None
            self._sdk = {
                "MV_FRAME_OUT_INFO_EX": FakeFrameInfo,
                "PixelType_Gvsp_Mono8": 0x01080001,
            }
            self.cam = FakeCam()
            self.opened = True
            self.grabbing = True
            self.recording = True
            self.payload_size = 4

        def _set_command(self, key: str) -> None:
            return None

        def _call_variants(self, func, variants, func_name: str):
            frame_info = variants[0][2]
            frame_info.nWidth = 2
            frame_info.nHeight = 2
            frame_info.nFrameLen = 4
            frame_info.enPixelType = 0x01080001
            frame_info.nFrameNum = 18
            return 0

        def _input_record_frame(self, data_buf, frame_len: int) -> None:
            self.input_frame_len = frame_len

    cam = GoodPixelRecordingController()
    frame = cam._record_one_frame_unlocked(timeout_ms=1)

    assert cam.input_frame_len == 4
    assert frame.pixel_type == 0x01080001
    assert frame.frame_len == 4


def test_camera_validator_rejects_invalid_sdk_path_and_unsupported_format(tmp_path) -> None:
    sdk_dir = tmp_path / "MvImport"
    sdk_dir.mkdir()
    cfg = {
        "camera": {
            "mvs_python_dir": str(sdk_dir),
            "device_index": 0,
            "serial_number": "DA8583237",
            "ip": "999.0.0.1",
            "resolution": {"width": 5120, "height": 5120, "allow_downscale": True},
            "exposure_us": 5000,
            "gain": 0.0,
            "objective_settings": {"4x": {"exposure_us": 5000, "gain": 0.0}},
            "trigger_mode": "hardware",
            "pixel_format": "rgb8",
            "save_format": "bmp",
            "save_options": {"create_dir_if_missing": True, "overwrite": True},
        }
    }

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_camera_config(cfg, require_top_level=True)

    message = str(exc_info.value)
    assert "missing MvCameraControl_class.py" in message
    assert "camera.ip" in message
    assert "camera.trigger_mode" in message
    assert "camera.pixel_format" in message


def test_camera_validator_requires_objective_settings_coverage(tmp_path) -> None:
    sdk_dir = tmp_path / "MvImport"
    sdk_dir.mkdir()
    (sdk_dir / "MvCameraControl_class.py").write_text("", encoding="utf-8")
    cfg = {
        "camera": {
            "mvs_python_dir": str(sdk_dir),
            "device_index": 0,
            "serial_number": None,
            "ip": "192.168.0.253",
            "resolution": {"width": 5120, "height": 5120, "allow_downscale": True},
            "exposure_us": 5000,
            "gain": 0.0,
            "objective_settings": {
                "4x": {"exposure_us": 5000, "gain": 0.0},
                "20x": {"exposure_us": 30000, "gain": 0.0},
            },
            "trigger_mode": "software",
            "pixel_format": "mono8",
            "save_format": "bmp",
            "save_options": {"create_dir_if_missing": True, "overwrite": True},
        }
    }

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_camera_config(cfg, require_top_level=True, objective_names={"4x", "10x"})

    message = str(exc_info.value)
    assert "camera.objective_settings.10x" in message
    assert "camera.objective_settings.20x" in message


def test_resolve_mvs_python_dir_prefers_canonical_field() -> None:
    assert resolve_mvs_python_dir({"mvs_python_dir": "C:/canonical", "mvs_sdk_path": "C:/legacy"}) == "C:/canonical"
    assert resolve_mvs_python_dir({"mvs_sdk_path": "C:/legacy"}) == "C:/legacy"


def test_project_autofocus_config_passes_machine_validation() -> None:
    validate_autofocus_file(
        "config/autofocus.yaml",
        objectives_path="config/objectives.yaml",
        camera_path="config/camera.yaml",
    )


def test_autofocus_validator_rejects_unsafe_or_stale_fields(tmp_path) -> None:
    sdk_dir = tmp_path / "MvImport"
    sdk_dir.mkdir()
    (sdk_dir / "MvCameraControl_class.py").write_text("", encoding="utf-8")
    cfg = {
        "enabled": True,
        "trigger": {
            "after_objective_switch": True,
            "always_before_capture": False,
            "always_before_capture_objectives": [],
            "scope": "once_per_well",
            "run_at": "before_first_capture_after_stage_move",
        },
        "mode": {"preview": True},
        "camera": {
            "backend": "mvs",
            "ip": "192.168.0.253",
            "net_export_ip": "192.168.0.10",
            "mvs_python_dir": str(sdk_dir),
            "exposure_auto": False,
            "objective_settings": {
                "4x": {"exposure_auto": False, "exposure_time_us": 5000},
            },
        },
        "motor": {
            "type": "modbus",
            "port": "COM3",
            "baudrate": 115200,
            "focus_slave": 3,
            "objective": "4x",
            "objective_ranges": {
                "4x": {"min_pos": 100, "max_pos": 200},
            },
            "min_pos": 0,
            "profile_vel": 50000,
            "profile_acc": 50000,
            "profile_dec": 50000,
        },
        "focus": {
            "tol": 100,
            "max_iter": 10,
            "settle_ms": 300,
            "center_roi": 0.6,
            "downsample": 0.5,
        },
        "output": {
            "timestamp_folder": True,
            "image_path": str(tmp_path / "sharpest.png"),
            "log_path": str(tmp_path / "focus_log.csv"),
        },
    }
    objectives_cfg = {
        "objectives": {
            "4x": {"switch": {"focus_target_pos": 50}},
            "10x": {"switch": {"focus_target_pos": 150}},
        },
        "hardware": {
            "modbus": {"port": "COM3", "baudrate": 115200},
            "focus_axis": {"slave": 3},
        },
    }
    camera_cfg = {
        "camera": {
            "mvs_python_dir": str(sdk_dir),
            "ip": "192.168.0.253",
            "objective_settings": {
                "4x": {"exposure_us": 5000},
                "10x": {"exposure_us": 20000},
            },
        }
    }

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_autofocus_config(cfg, objectives_cfg=objectives_cfg, camera_cfg=camera_cfg)

    message = str(exc_info.value)
    assert "autofocus.mode.preview" in message
    assert "autofocus.camera.objective_settings.10x" in message
    assert "autofocus.motor.objective_ranges.10x" in message
    assert "autofocus.motor.objective_ranges.4x" in message
    assert "autofocus.motor.min_pos" in message


def test_plates_validator_rejects_misplaced_runtime_guard_and_legacy_fields() -> None:
    base_plate = {
        "rows": 1,
        "cols": 1,
        "a1_start": {"x": 0, "y": 0},
        "well_diameter_mm": 1.0,
        "well_gap_mm": 0.0,
        "pulses_per_mm": 100,
        "row_stage_sign": -1,
        "col_stage_sign": -1,
        "x_stage_sign_for_view_down": -1,
        "y_stage_sign_for_view_right": -1,
        "stage_limits": {
            "enabled": True,
            "x_min": 0,
            "x_max": 1000,
            "y_min": 0,
            "y_max": 1000,
            "safety_margin": 0,
        },
        "runtime_guard": {
            "enabled": True,
            "stuck_min_expected_move_pulse": 1,
            "stuck_max_actual_move_pulse": 1,
            "max_err_to_target_pulse": 1,
            "abort_on_motion_failure": True,
        },
    }
    cfg = {
        "plates": {
            "runtime_guard": {"enabled": True},
            "6-well": {**base_plate, "row_stage_sign": 0},
            "12-well": {**base_plate, "point_12": [0, 0]},
            "24-well": base_plate,
            "48-well": base_plate,
        }
    }

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_plates_config(cfg)

    message = str(exc_info.value)
    assert "plates.runtime_guard" in message
    assert "plates.6-well.row_stage_sign" in message
    assert "plates.12-well.point_12" in message


def test_handoff_arrival_tolerance_rejects_large_error() -> None:
    _check_arrival_tolerance("X", err=3000, tolerance=3000, target=100)
    _check_arrival_tolerance("Y", err=-3000, tolerance=3000, target=100)

    with pytest.raises(HandoffError, match="到位误差超过阈值"):
        _check_arrival_tolerance("X", err=3001, tolerance=3000, target=100)


def test_project_handoff_config_passes_machine_validation() -> None:
    validate_handoff_file("config/handoff.yaml")


def test_handoff_validator_rejects_invalid_hardware_and_action_reference() -> None:
    cfg = {
        "handoff": {
            "hardware": {
                "modbus": {"port": "COM3", "baudrate": 115200},
                "x_axis": {"slave": 1},
                "y_axis": {"slave": 1},
            },
            "points": {
                "robot_exchange": {
                    "x": 0,
                    "y": 7500000,
                    "profile_vel": 500000,
                    "profile_acc": 100000,
                    "profile_dec": 100000,
                    "timeout_s": 120.0,
                    "settle_s": 0.5,
                    "arrival_tolerance_pulse": -1,
                }
            },
            "actions": {
                "load_in": {
                    "point": "missing_point",
                    "ready_state": "ready_for_robot_place",
                    "message": "ready",
                },
                "unload_out": {
                    "point": "robot_exchange",
                    "ready_state": "ready_for_robot_pick",
                    "message": "ready",
                },
            },
        }
    }

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_handoff_config(cfg)

    message = str(exc_info.value)
    assert "x_axis.slave and y_axis.slave must be different" in message
    assert "handoff.points.robot_exchange.arrival_tolerance_pulse" in message
    assert "handoff.actions.load_in.point" in message
