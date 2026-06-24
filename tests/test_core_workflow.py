from __future__ import annotations

import asyncio
import json
import threading
import textwrap

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
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


def test_api_server_hardware_guard_blocks_parallel_operations() -> None:
    from workflow import api_server

    with api_server._HARDWARE_OPERATION_LOCK:
        api_server._HARDWARE_OWNER = None
        api_server._CAMERA_RECORD_OWNER = None

    api_server._acquire_hardware_operation("task", "task-a")
    try:
        with pytest.raises(HTTPException) as exc:
            api_server._acquire_hardware_operation("stage_reciprocation", "stage_reciprocation")
        assert exc.value.status_code == 409
        assert exc.value.detail["error_code"] == "HARDWARE_BUSY"
        assert exc.value.detail["message"] == "硬件正在执行其他任务，请稍后重试"
    finally:
        api_server._release_hardware_operation("task", "task-a")


def test_api_server_file_logging_is_configured_once() -> None:
    from workflow import api_server

    log_path = str(api_server.API_LOG_PATH.resolve(strict=False))

    api_server._configure_api_file_logging()
    api_server._configure_api_file_logging()

    api_handlers = [
        handler for handler in api_server.logger.handlers if getattr(handler, "_colony_api_log_path", None) == log_path
    ]
    access_handlers = [
        handler for handler in api_server.access_logger.handlers if getattr(handler, "_colony_api_log_path", None) == log_path
    ]

    assert len(api_handlers) == 1
    assert len(access_handlers) == 1
    assert api_server.API_LOG_PATH == api_server.PROJECT_ROOT / "logs" / "api_server.log"


def test_atomic_write_json_retries_short_replace_contention(tmp_path, monkeypatch) -> None:
    from workflow import file_io

    output = tmp_path / "result.json"
    original_replace = file_io.os.replace
    calls = {"count": 0}

    def flaky_replace(src, dst):
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError("temporary handle contention")
        return original_replace(src, dst)

    monkeypatch.setattr(file_io.os, "replace", flaky_replace)

    file_io.atomic_write_json(output, {"status": "success"}, attempts=2, sleep_s=0)

    assert calls["count"] == 2
    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "success"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_read_json_with_retry_recovers_from_partial_json(monkeypatch) -> None:
    from workflow import file_io

    reads = iter(["{", '{"status": "success"}'])

    def flaky_read_text(_path, **_kwargs):
        return next(reads)

    monkeypatch.setattr(file_io, "read_text_with_retry", flaky_read_text)

    assert file_io.read_json_with_retry("result.json", attempts=2, sleep_s=0) == {"status": "success"}


def test_api_server_get_task_result_uses_retry_json_reader(tmp_path, monkeypatch) -> None:
    from workflow import api_server

    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path / "task_index"))
    monkeypatch.setattr(api_server, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(api_server, "OUTPUTS_ROOT", tmp_path / "outputs")
    result_path = tmp_path / "result.json"
    api_server._write_task_record(
        {
            "task_id": "result-task",
            "status": "success",
            "result_json_path": str(result_path),
            "updated_at": api_server._utc_now(),
        }
    )
    result_path.write_text('{"status": "success"}', encoding="utf-8")

    original_reader = api_server.read_json_with_retry
    calls = []

    def tracking_reader(path, **kwargs):
        calls.append(str(path))
        return original_reader(path, **kwargs)

    monkeypatch.setattr(api_server, "read_json_with_retry", tracking_reader)

    assert api_server.get_task_result("result-task") == {"status": "success"}
    assert any(path.endswith("result.json") for path in calls)


def test_api_server_hardware_guard_allows_task_with_active_camera_record() -> None:
    from workflow import api_server

    with api_server._HARDWARE_OPERATION_LOCK:
        api_server._HARDWARE_OWNER = None
        api_server._CAMERA_RECORD_OWNER = None

    api_server._acquire_hardware_operation("camera_record", "recording.avi")
    try:
        api_server._acquire_hardware_operation("task", "allowed-task")
        owners = api_server._current_hardware_owners()
        assert {owner["kind"] for owner in owners} == {"camera_record", "task"}
        assert {owner["operation_id"] for owner in owners} == {"recording.avi", "allowed-task"}
    finally:
        api_server._release_hardware_operation("task", "allowed-task")
        api_server._release_hardware_operation("camera_record", "recording.avi")


def test_api_server_hardware_guard_releases_terminal_task_record(tmp_path, monkeypatch) -> None:
    from workflow import api_server

    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path))
    with api_server._HARDWARE_OPERATION_LOCK:
        api_server._HARDWARE_OWNER = None
        api_server._CAMERA_RECORD_OWNER = None
    api_server._write_task_record(
        {
            "task_id": "finished-task",
            "status": "success",
            "updated_at": api_server._utc_now(),
        }
    )
    with api_server._HARDWARE_OPERATION_LOCK:
        api_server._HARDWARE_OWNER = {
            "kind": "task",
            "operation_id": "finished-task",
            "started_at": api_server._utc_now(),
            "sync_after_monotonic": 0.0,
        }

    assert api_server._current_hardware_owner() is None


def test_api_server_startup_recovery_marks_active_tasks_interrupted(tmp_path, monkeypatch) -> None:
    from workflow import api_server

    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path))
    api_server._write_task_record(
        {
            "task_id": "running-task",
            "status": "running",
            "progress": 25,
            "updated_at": api_server._utc_now(),
            "wells": {
                "A1": {"status": "running", "message": "capturing"},
                "A2": {"status": "queued", "message": "waiting"},
                "A3": {"status": "success", "message": "completed"},
            },
        }
    )
    api_server._write_task_record(
        {
            "task_id": "success-task",
            "status": "success",
            "message": "task completed",
            "updated_at": api_server._utc_now(),
        }
    )

    stats = api_server._recover_interrupted_task_records()

    assert stats["interrupted"] == 1
    running = api_server._read_task_record("running-task")
    assert running["status"] == "interrupted"
    assert running["previous_status"] == "running"
    assert running["progress"] == 25
    assert running["finished_at"]
    assert running["interrupted_at"]
    assert "API 服务启动" in running["message"]
    assert running["wells"]["A1"]["status"] == "interrupted"
    assert running["wells"]["A1"]["previous_status"] == "running"
    assert running["wells"]["A2"]["status"] == "interrupted"
    assert running["wells"]["A3"]["status"] == "success"

    success = api_server._read_task_record("success-task")
    assert success["status"] == "success"
    assert "previous_status" not in success


def test_api_server_request_paths_are_normalized_under_project_roots() -> None:
    from workflow import api_server

    req = api_server.ExecuteTaskRequest(
        task={
            "task_id": "path-task",
            "capture": {"save_dir": "data/captures/path-task"},
            "scan": {"output_json": "outputs/path-task/scan_result.json"},
            "detect": {
                "input_scan_result_json": "data/captures/path-task/scan_result.json",
                "output_json": "data/captures/path-task/detect_result.json",
            },
            "compensate": {
                "input_detect_json": "data/captures/path-task/detect_result.json",
                "output_json": "outputs/path-task/compensate_result.json",
                "closed_loop": {"save_dir": "data/captures/path-task/closed_loop"},
            },
            "output": {
                "result_json": "data/captures/path-task/result.json",
                "detect_json": "outputs/path-task/result_detect.json",
            },
        },
        camera_path="config/camera.yaml",
        objectives_path="config/objectives.yaml",
        plates_path="config/plates.yaml",
        dump_json="data/captures/path-task/api_result.json",
    )

    normalized = api_server._normalize_execute_task_request(req)

    assert normalized.camera_path == str((api_server.CONFIG_ROOT / "camera.yaml").resolve(strict=False))
    assert normalized.objectives_path == str((api_server.CONFIG_ROOT / "objectives.yaml").resolve(strict=False))
    assert normalized.plates_path == str((api_server.CONFIG_ROOT / "plates.yaml").resolve(strict=False))
    assert normalized.dump_json == str((api_server.DATA_ROOT / "captures" / "path-task" / "api_result.json").resolve(strict=False))
    assert normalized.task["capture"]["save_dir"] == str((api_server.DATA_ROOT / "captures" / "path-task").resolve(strict=False))
    assert normalized.task["scan"]["output_json"] == str((api_server.OUTPUTS_ROOT / "path-task" / "scan_result.json").resolve(strict=False))
    assert normalized.task["compensate"]["closed_loop"]["save_dir"] == str(
        (api_server.DATA_ROOT / "captures" / "path-task" / "closed_loop").resolve(strict=False)
    )


def test_api_server_request_paths_reject_outside_project_roots() -> None:
    from workflow import api_server

    with pytest.raises(HTTPException) as config_exc:
        api_server._normalize_execute_task_request(
            api_server.ExecuteTaskRequest(
                task={"task_id": "bad-config"},
                camera_path="C:/Windows/camera.yaml",
            )
        )
    assert config_exc.value.status_code == 400
    assert config_exc.value.detail["error_code"] == "PATH_OUT_OF_ALLOWED_ROOT"

    with pytest.raises(HTTPException) as output_exc:
        api_server._normalize_execute_task_request(
            api_server.ExecuteTaskRequest(
                task={
                    "task_id": "bad-output",
                    "capture": {"save_dir": "../outside-captures"},
                }
            )
        )
    assert output_exc.value.status_code == 400
    assert output_exc.value.detail["error_code"] == "PATH_OUT_OF_ALLOWED_ROOT"


def test_api_server_camera_record_config_error_returns_400(monkeypatch) -> None:
    from workflow import api_server

    with api_server._HARDWARE_OPERATION_LOCK:
        api_server._HARDWARE_OWNER = None
        api_server._CAMERA_RECORD_OWNER = None

    def fail_load_settings(_req):
        raise ValueError("bad camera config")

    monkeypatch.setattr(api_server, "_load_camera_settings_for_recording", fail_load_settings)

    with pytest.raises(HTTPException) as exc:
        api_server.start_camera_record(
            api_server.CameraRecordStartRequest(save_path="data/camera_records/config-error.avi")
        )

    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "CAMERA_RECORD_CONFIG_INVALID"
    assert exc.value.detail["message"] == "相机录像配置无效，请检查 camera.yaml 或请求参数"
    assert api_server._current_hardware_owners() == []


def test_api_server_camera_record_request_rejects_invalid_ranges() -> None:
    from workflow import api_server

    with pytest.raises(ValidationError):
        api_server.CameraRecordStartRequest(fps=0)
    with pytest.raises(ValidationError):
        api_server.CameraRecordStartRequest(bitrate_kbps=0)
    with pytest.raises(ValidationError):
        api_server.CameraRecordStartRequest(timeout_ms=-1)
    with pytest.raises(ValidationError):
        api_server.CameraRecordStartRequest(device_index=-1)
    with pytest.raises(ValidationError):
        api_server.CameraRecordStartRequest(exposure_us=0)
    with pytest.raises(ValidationError):
        api_server.CameraRecordStartRequest(gain=-0.1)


def test_api_server_request_validation_error_returns_public_error() -> None:
    from workflow import api_server

    class RequestStub:
        class UrlStub:
            path = "/api/camera/record/start"

        url = UrlStub()

    response = asyncio.run(
        api_server._request_validation_exception_handler(
            RequestStub(),
            RequestValidationError(
                [{"type": "greater_than", "loc": ("body", "fps"), "msg": "Input should be greater than 0"}],
                body={"save_path": "data/camera_records/invalid-fps.avi", "fps": 0},
            ),
        )
    )

    assert response.status_code == 422
    assert json.loads(response.body)["detail"] == {
        "error_code": "REQUEST_VALIDATION_FAILED",
        "message": "请求参数不合法",
    }


def test_api_server_http_error_handler_sanitizes_plain_detail() -> None:
    from workflow import api_server

    class RequestStub:
        class UrlStub:
            path = "/missing"

        url = UrlStub()

    response = asyncio.run(
        api_server._http_exception_handler(
            RequestStub(),
            api_server.HTTPException(status_code=404, detail="C:/secret/config.yaml"),
        )
    )

    assert response.status_code == 404
    assert json.loads(response.body)["detail"] == {
        "error_code": "HTTP_404",
        "message": "请求的资源不存在",
    }


def test_api_server_stage_reciprocation_request_rejects_invalid_ranges() -> None:
    from workflow import api_server

    with pytest.raises(ValidationError):
        api_server.StageReciprocationStartRequest(profile_vel=0)
    with pytest.raises(ValidationError):
        api_server.StageReciprocationStartRequest(profile_acc=0)
    with pytest.raises(ValidationError):
        api_server.StageReciprocationStartRequest(profile_dec=0)
    with pytest.raises(ValidationError):
        api_server.StageReciprocationStartRequest(poll_s=0)
    with pytest.raises(ValidationError):
        api_server.StageReciprocationStartRequest(move_timeout_s=0)
    with pytest.raises(ValidationError):
        api_server.StageReciprocationStartRequest(max_cycles=0)
    with pytest.raises(ValidationError):
        api_server.StageReciprocationStartRequest(x_min=10, x_max=10)
    with pytest.raises(ValidationError):
        api_server.StageReciprocationStartRequest(safety_margin=10_000_000)
    with pytest.raises(ValidationError):
        api_server.StageReciprocationStartRequest(point_a_x=999_999_999)


def test_api_server_cancel_task_marks_record_and_sets_event(tmp_path, monkeypatch) -> None:
    from workflow import api_server

    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path))
    cancel_event = threading.Event()
    with api_server._TASK_CANCEL_LOCK:
        api_server._TASK_CANCEL_EVENTS.clear()
    api_server._register_task_cancel_event("cancel-me", cancel_event)
    api_server._write_task_record(
        {
            "task_id": "cancel-me",
            "status": "running",
            "progress": 40,
            "updated_at": api_server._utc_now(),
        }
    )

    result = api_server.cancel_task("cancel-me")

    assert result["status"] == "cancel_requested"
    assert result["cancel_requested"] is True
    assert cancel_event.is_set()
    record = api_server._read_task_record("cancel-me")
    assert record["status"] == "running"
    assert record["cancel_requested"] is True
    assert record["cancel_requested_at"]

    api_server._unregister_task_cancel_event("cancel-me")


def test_api_server_run_task_async_writes_canceled_record(tmp_path, monkeypatch) -> None:
    from workflow import api_server
    from workflow.task_control import TaskCanceled

    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path))
    task = {"task_id": "worker-cancel", "task_type": "capture"}
    req = api_server.ExecuteTaskRequest(task=task)
    api_server._write_task_record(api_server._build_accepted_record(task, None, True))

    def fake_execute_task_request(*_args, **_kwargs):
        raise TaskCanceled("operator canceled")

    monkeypatch.setattr(api_server, "execute_task_request", fake_execute_task_request)

    api_server._run_task_async(task, req, threading.Event())

    record = api_server._read_task_record("worker-cancel")
    assert record["status"] == "canceled"
    assert record["cancel_requested"] is True
    assert record["canceled_at"]
    assert record["cancel_reason"] == "operator canceled"


def test_scan_executor_checks_cancel_before_stage_move() -> None:
    from workflow import scan_executor
    from workflow.task_control import TaskCanceled

    params = {
        "task_id": "scan-cancel",
        "task_type": "capture",
        "plate_type": "24-well",
        "well_name": "A1",
        "objective_name": "4x",
        "motion": {},
        "_cancel_check": lambda: True,
    }
    plan = {
        "points": [
            {
                "index": 1,
                "row_index": 0,
                "col_index": 0,
                "stage_x_target": 1,
                "stage_y_target": 2,
            }
        ],
        "reference": {},
        "scan_config": {},
    }

    with pytest.raises(TaskCanceled):
        scan_executor.execute_scan_capture({"plate": {}}, params, plan)


def test_detect_executor_checks_cancel_before_image_detection() -> None:
    from workflow import detect_executor
    from workflow.task_control import TaskCanceled

    scan_result = {
        "scan_config": {"fov_mm": {"width": 1.0, "height": 1.0}},
        "captures": [
            {
                "index": 1,
                "capture_result": {"saved_path": "not-read-before-cancel.bmp"},
            }
        ],
    }
    params = {
        "task_id": "detect-cancel",
        "plate_type": "24-well",
        "well_name": "A1",
        "objective_name": "4x",
        "_cancel_check": lambda: True,
    }

    with pytest.raises(TaskCanceled):
        detect_executor.execute_detect_on_scan_result({"task": {}}, params, scan_result)


def test_compensate_executor_checks_cancel_before_selection() -> None:
    from workflow import compensate_executor
    from workflow.task_control import TaskCanceled

    params = {
        "task_id": "compensate-cancel",
        "plate_type": "24-well",
        "well_name": "A1",
        "objective_name": "4x",
        "_cancel_check": lambda: True,
    }

    with pytest.raises(TaskCanceled):
        compensate_executor.execute_compensate_on_detect_result({}, params, {"images": []})


def test_stage_executor_move_uses_timeout_and_arrival_tolerance(monkeypatch) -> None:
    from workflow import stage_executor

    class FakeClient:
        events = []

        REG_CURRENT_POS = 968
        REG_CMD_POS = 966
        REG_TARGET_POS = 999
        REG_PROFILE_VEL_HIGH = 1016
        REG_PROFILE_ACC_HIGH = 1020
        REG_PROFILE_DEC_HIGH = 1022
        CMD_ENABLE_OPERATION = 0x0F
        STAT_FAULT = 0x0008

        def __init__(self, *args, **kwargs):
            self.positions = {1: 0, 2: 0}
            self.targets = {1: 0, 2: 0}
            self.quick_stops = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return None

        def _read_32bit(self, slave, reg):
            if reg == self.REG_CMD_POS:
                return self.targets[slave]
            return self.positions[slave]

        def _read_statusword(self, _slave):
            return 4

        def _write_32bit(self, slave, reg, value):
            if reg == self.REG_TARGET_POS:
                self.targets[slave] = int(value)
                FakeClient.events.append(("write_target", slave, int(value)))
            else:
                FakeClient.events.append(("write_param", slave, reg, int(value)))
            return True

        def _write_controlword(self, slave, value):
            FakeClient.events.append(("controlword", slave, int(value)))
            if int(value) == (self.CMD_ENABLE_OPERATION | 0x10):
                self.positions[slave] = self.targets[slave]
            return True

        def _restore_enabled_state(self, slave):
            FakeClient.events.append(("restore", slave))
            return True

        def quick_stop(self, slave):
            self.quick_stops.append(slave)
            FakeClient.events.append(("quick_stop", slave))
            return True

    class FakeMotor:
        MODE_PROFILE_POSITION = 0x01

        def __init__(self, client, slave):
            self.client = client
            self.slave = slave

        def _ensure_mode_and_enable(self, target_mode, auto_enable=True):
            return target_mode == self.MODE_PROFILE_POSITION and auto_enable is True

    monkeypatch.setattr(stage_executor, "ModbusRTUClient", FakeClient)
    monkeypatch.setattr(stage_executor, "MotorManager", FakeMotor)

    result = stage_executor.move_to_absolute(
        port="COM3",
        x_target=100,
        y_target=200,
        profile_vel=10,
        profile_acc=20,
        profile_dec=30,
        settle_s=0,
        timeout_s=7.5,
        arrival_tolerance_pulse=0,
        stage_limits={
            "enabled": True,
            "x_min": 0,
            "x_max": 1000,
            "y_min": 0,
            "y_max": 1000,
            "safety_margin": 0,
        },
    )

    assert result["err_to_target"] == {"x": 0, "y": 0}
    assert result["motion_params"]["timeout_s"] == 7.5
    assert result["motion_params"]["move_mode"] == "simultaneous_pp"
    first_trigger_index = next(
        index
        for index, event in enumerate(FakeClient.events)
        if event == ("controlword", 1, 0x1F) or event == ("controlword", 2, 0x1F)
    )
    assert ("write_target", 1, 100) in FakeClient.events[:first_trigger_index]
    assert ("write_target", 2, 200) in FakeClient.events[:first_trigger_index]


def test_stage_executor_rejects_target_outside_stage_limits(monkeypatch) -> None:
    from workflow import stage_executor

    class ShouldNotOpenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("client should not be opened for an out-of-range target")

    monkeypatch.setattr(stage_executor, "ModbusRTUClient", ShouldNotOpenClient)

    with pytest.raises(stage_executor.StageMotionError, match="outside safe range"):
        stage_executor.move_to_absolute(
            port="COM3",
            x_target=2000,
            y_target=100,
            profile_vel=10,
            profile_acc=10,
            profile_dec=10,
            settle_s=0,
            stage_limits={
                "enabled": True,
                "x_min": 0,
                "x_max": 1000,
                "y_min": 0,
                "y_max": 1000,
                "safety_margin": 0,
            },
        )


def test_stage_executor_quick_stops_xy_when_axis_move_fails(monkeypatch) -> None:
    from workflow import stage_executor

    class FakeClient:
        last_instance = None
        fail_slave = 2
        REG_CURRENT_POS = 968
        REG_CMD_POS = 966
        REG_TARGET_POS = 999
        REG_PROFILE_VEL_HIGH = 1016
        REG_PROFILE_ACC_HIGH = 1020
        REG_PROFILE_DEC_HIGH = 1022
        CMD_ENABLE_OPERATION = 0x0F
        STAT_FAULT = 0x0008

        def __init__(self, *args, **kwargs):
            self.positions = {1: 0, 2: 0}
            self.targets = {1: 0, 2: 0}
            self.quick_stops = []
            FakeClient.last_instance = self

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return None

        def _read_32bit(self, slave, reg):
            if reg == self.REG_CMD_POS:
                return self.targets[slave]
            return self.positions[slave]

        def _read_statusword(self, _slave):
            return 4

        def _write_32bit(self, slave, reg, value):
            if slave == self.fail_slave and reg == self.REG_TARGET_POS:
                return False
            if reg == self.REG_TARGET_POS:
                self.targets[slave] = int(value)
            return True

        def _write_controlword(self, slave, value):
            if int(value) == (self.CMD_ENABLE_OPERATION | 0x10):
                self.positions[slave] = self.targets[slave]
            return True

        def _restore_enabled_state(self, _slave):
            return True

        def quick_stop(self, slave):
            self.quick_stops.append(slave)
            return True

    class FakeMotor:
        MODE_PROFILE_POSITION = 0x01

        def __init__(self, client, slave):
            self.client = client
            self.slave = slave

        def _ensure_mode_and_enable(self, target_mode, auto_enable=True):
            return target_mode == self.MODE_PROFILE_POSITION and auto_enable is True

    monkeypatch.setattr(stage_executor, "ModbusRTUClient", FakeClient)
    monkeypatch.setattr(stage_executor, "MotorManager", FakeMotor)

    with pytest.raises(stage_executor.StageMotionError, match="y axis failed to set target position"):
        stage_executor.move_to_absolute(
            port="COM3",
            x_target=100,
            y_target=200,
            profile_vel=10,
            profile_acc=10,
            profile_dec=10,
            settle_s=0,
        )

    assert FakeClient.last_instance is not None
    assert FakeClient.last_instance.quick_stops == [1, 2]


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


def test_handoff_executor_uses_stage_executor_move_entry(monkeypatch) -> None:
    from workflow import handoff_executor

    calls = []

    def fake_move_to_absolute(**kwargs):
        calls.append(kwargs)
        return {
            "before": {
                "x": {"current_pos": 0},
                "y": {"current_pos": 0},
            },
            "after": {
                "x": {"current_pos": kwargs["x_target"], "target_pos": kwargs["x_target"]},
                "y": {"current_pos": kwargs["y_target"], "target_pos": kwargs["y_target"]},
            },
            "err_to_target": {"x": 0, "y": 0},
            "motion_params": {"move_mode": "simultaneous_pp"},
        }

    monkeypatch.setattr(handoff_executor, "move_to_absolute", fake_move_to_absolute)

    result = handoff_executor.execute_handoff_task(
        {
            "task_id": "handoff-task",
            "plate_type": "24-well",
            "handoff": {"action": "load_in"},
        },
        {
            "handoff": {
                "hardware": {
                    "modbus": {"port": "COM3", "baudrate": 115200},
                    "x_axis": {"slave": 1},
                    "y_axis": {"slave": 2},
                },
                "points": {
                    "robot_exchange": {
                        "x": 100,
                        "y": 200,
                        "profile_vel": 10,
                        "profile_acc": 20,
                        "profile_dec": 30,
                        "settle_s": 0,
                        "timeout_s": 5,
                        "poll_s": 0.01,
                        "arrival_tolerance_pulse": 3,
                    },
                },
                "actions": {
                    "load_in": {
                        "point": "robot_exchange",
                        "ready_state": "ready_for_load",
                    },
                },
            },
        },
    )

    assert result["status"] == "success"
    assert len(calls) == 1
    assert calls[0]["port"] == "COM3"
    assert calls[0]["x_target"] == 100
    assert calls[0]["y_target"] == 200
    assert calls[0]["arrival_tolerance_pulse"] == 3
    assert calls[0]["poll_s"] == 0.01
    assert result["move_result"]["motion_params"]["move_mode"] == "simultaneous_pp"


def test_stage_reciprocation_move_target_uses_stage_executor_entry(monkeypatch) -> None:
    from workflow import stage_reciprocation

    calls = []

    def fake_move_to_absolute(**kwargs):
        calls.append(kwargs)
        kwargs["progress_callback"]({"x": 11, "y": 22})
        return {
            "before": {
                "x": {"current_pos": 0},
                "y": {"current_pos": 0},
            },
            "after": {
                "x": {"current_pos": kwargs["x_target"], "target_pos": kwargs["x_target"]},
                "y": {"current_pos": kwargs["y_target"], "target_pos": kwargs["y_target"]},
            },
            "err_to_target": {"x": 0, "y": 0},
            "motion_params": {"move_mode": "simultaneous_pp"},
        }

    monkeypatch.setattr(stage_reciprocation, "move_to_absolute", fake_move_to_absolute)

    controller = StageReciprocationController()
    cfg = {
        "port": "COM3",
        "baudrate": 115200,
        "x_slave": 1,
        "y_slave": 2,
        "profile_vel": 10,
        "profile_acc": 20,
        "profile_dec": 30,
        "settle_s": 0,
        "move_timeout_s": 5,
        "poll_s": 0.01,
        "arrival_tolerance": 3,
        "limits": {
            "enabled": True,
            "x_min": 0,
            "x_max": 1000,
            "y_min": 0,
            "y_max": 1000,
            "safety_margin": 0,
        },
    }

    move = controller._move_target({"well_name": "B2", "x": 100, "y": 200}, cfg, cycle=1)

    assert len(calls) == 1
    assert calls[0]["stop_event"] is controller._stop_event
    assert calls[0]["progress_callback"] == controller._update_current_pos
    assert calls[0]["stage_limits"] == cfg["limits"]
    assert calls[0]["x_target"] == 100
    assert calls[0]["y_target"] == 200
    assert move["motion_params"]["move_mode"] == "simultaneous_pp"
    status = controller.status()
    assert status["last_move"]["target"]["well_name"] == "B2"
    assert status["current_pos"] == {"x": 100, "y": 200}


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
