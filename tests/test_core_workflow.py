from __future__ import annotations

import asyncio
import json
import threading
import textwrap
from pathlib import Path

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
import pytest

from workflow.config_validator import (
    ConfigValidationError,
    load_yaml_unique,
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


def _fake_mvs_sdk_dir(tmp_path: Path) -> Path:
    from workflow.config_validator import MVS_REQUIRED_PYTHON_FILES

    sdk_dir = tmp_path / "MvImport"
    sdk_dir.mkdir()
    for required_file in MVS_REQUIRED_PYTHON_FILES:
        (sdk_dir / required_file).write_text("# regression-test stub\n", encoding="utf-8")
    return sdk_dir


def test_api_server_hardware_guard_blocks_parallel_operations() -> None:
    from workflow import hardware_guard

    hardware_guard.reset_hardware_owners()

    hardware_guard.acquire_hardware_operation("task", "task-a")
    try:
        with pytest.raises(hardware_guard.HardwareGuardError) as exc:
            hardware_guard.acquire_hardware_operation("stage_reciprocation", "stage_reciprocation")
        assert exc.value.status_code == 409
        assert exc.value.error_code == "HARDWARE_BUSY"
        assert exc.value.message
    finally:
        hardware_guard.release_hardware_operation("task", "task-a")


def test_api_server_file_logging_is_configured_once() -> None:
    from workflow import api_errors, api_server, file_io, path_guard

    log_path = str(api_errors.API_LOG_PATH.resolve(strict=False))

    api_server._configure_api_file_logging()
    api_server._configure_api_file_logging()

    api_handlers = [
        handler for handler in api_server.logger.handlers if getattr(handler, "_colony_api_log_path", None) == log_path
    ]
    access_handlers = [
        handler for handler in api_errors.access_logger.handlers if getattr(handler, "_colony_api_log_path", None) == log_path
    ]
    file_io_handlers = [
        handler for handler in file_io.logger.handlers if getattr(handler, "_colony_api_log_path", None) == log_path
    ]

    assert len(api_handlers) == 1
    assert len(access_handlers) == 1
    assert len(file_io_handlers) == 1
    assert api_errors.API_LOG_PATH == path_guard.PROJECT_ROOT / "logs" / "api_server.log"


def test_process_guard_rejects_multi_worker_configuration() -> None:
    from workflow.process_guard import SingleWorkerGuardError, assert_single_worker_config

    assert assert_single_worker_config({"COLONY_API_WORKERS": "1"}) == ("COLONY_API_WORKERS", 1)

    with pytest.raises(SingleWorkerGuardError) as exc:
        assert_single_worker_config({"COLONY_API_WORKERS": "2"})
    assert exc.value.error_code == "MULTI_WORKER_NOT_SUPPORTED"
    assert "COLONY_API_WORKERS=2" in str(exc.value.log_detail)

    with pytest.raises(SingleWorkerGuardError) as conflict_exc:
        assert_single_worker_config({"COLONY_API_WORKERS": "1", "WEB_CONCURRENCY": "2"})
    assert conflict_exc.value.error_code == "MULTI_WORKER_NOT_SUPPORTED"
    assert "WEB_CONCURRENCY=2" in str(conflict_exc.value.log_detail)

    with pytest.raises(SingleWorkerGuardError) as invalid_exc:
        assert_single_worker_config({"WEB_CONCURRENCY": "two"})
    assert invalid_exc.value.error_code == "INVALID_WORKER_COUNT"


def test_process_guard_single_instance_lock_blocks_second_holder(tmp_path) -> None:
    from workflow.process_guard import SingleInstanceLock, SingleWorkerGuardError

    lock_path = tmp_path / "api_server.lock"
    first = SingleInstanceLock(lock_path, owner="first")
    second = SingleInstanceLock(lock_path, owner="second")

    first.acquire()
    try:
        assert first.acquired
        with pytest.raises(SingleWorkerGuardError) as exc:
            second.acquire()
        assert exc.value.error_code == "API_SERVER_ALREADY_RUNNING"
    finally:
        first.release()

    second.acquire()
    try:
        assert second.acquired
    finally:
        second.release()


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


def test_api_errors_sanitize_log_detail_redacts_paths_and_task_ids(monkeypatch) -> None:
    from workflow import api_errors, path_guard

    task_id = "patient-alpha-A1"
    detail = f"task_id={task_id} path={path_guard.DATA_ROOT / 'captures' / task_id / 'result.json'}"

    monkeypatch.setenv("COLONY_LOG_REDACT_SENSITIVE", "0")
    assert api_errors.sanitize_log_detail(detail) == detail

    monkeypatch.setenv("COLONY_LOG_REDACT_SENSITIVE", "1")
    sanitized = api_errors.sanitize_log_detail(detail)

    assert task_id not in sanitized
    assert "result.json" not in sanitized
    assert "task_id=<task:" in sanitized
    assert "<DATA_ROOT>/<redacted>" in sanitized


def test_file_io_logs_slow_retry_with_redacted_path(tmp_path, monkeypatch, caplog) -> None:
    import logging

    from workflow import file_io

    output = tmp_path / "task_index" / "secret-task.json"
    original_replace = file_io.os.replace
    calls = {"count": 0}

    def flaky_replace(src, dst):
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError("temporary handle contention")
        return original_replace(src, dst)

    monkeypatch.setattr(file_io.os, "replace", flaky_replace)
    monkeypatch.setenv("COLONY_FILE_IO_SLOW_WARNING_MS", "0")
    monkeypatch.setenv("COLONY_LOG_REDACT_SENSITIVE", "1")

    with caplog.at_level(logging.WARNING, logger="workflow.file_io"):
        file_io.atomic_write_json(output, {"status": "success"}, attempts=2, sleep_s=0)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "FILE_IO_SLOW" in messages
    assert "op=atomic_write_json" in messages
    assert "path_kind=task_record" in messages
    assert "last_error=PermissionError" in messages
    assert "secret-task" not in messages


def test_read_json_with_retry_recovers_from_partial_json(monkeypatch) -> None:
    from workflow import file_io

    reads = iter(["{", '{"status": "success"}'])

    def flaky_read_text(_path, **_kwargs):
        return next(reads)

    monkeypatch.setattr(file_io, "read_text_with_retry", flaky_read_text)

    assert file_io.read_json_with_retry("result.json", attempts=2, sleep_s=0) == {"status": "success"}


def test_task_store_create_accepted_record_is_single_locked_entrypoint(tmp_path, monkeypatch) -> None:
    from workflow import task_store

    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path))
    task = {"task_id": "atomic-task", "task_type": "capture", "objective": "10x"}

    first = task_store.create_accepted_task_record_if_allowed(task, None, True)

    assert first["status"] == "queued"
    assert first["task_id"] == "atomic-task"
    assert first["objective_name"] == "10x"
    assert first["request_task"]["objective_name"] == "10x"
    assert "objective" not in first["request_task"]

    with pytest.raises(task_store.TaskStoreError) as exc:
        task_store.create_accepted_task_record_if_allowed(task, None, True)
    assert exc.value.status_code == 409
    assert exc.value.error_code == "TASK_ALREADY_RUNNING"

    terminal = dict(first)
    terminal["status"] = "success"
    task_store.write_task_record(terminal)

    second = task_store.create_accepted_task_record_if_allowed(task, None, True)

    assert second["status"] == "queued"
    assert task_store.read_task_record("atomic-task")["status"] == "queued"


def test_task_runtime_normalizes_objective_alias_in_execute_request() -> None:
    from workflow import task_runtime
    from workflow.api_models import ExecuteTaskRequest

    req = ExecuteTaskRequest(
        task={
            "task_id": "objective-alias",
            "task_type": "capture",
            "objective": "10x",
            "capture": {"save_dir": "data/captures/objective-alias"},
        }
    )

    normalized = task_runtime.normalize_execute_task_request(req)

    assert normalized.task["objective_name"] == "10x"
    assert "objective" not in normalized.task


def test_run_task_build_pipeline_params_uses_objective_name() -> None:
    from workflow import run_task

    ctx = {
        "task": {
            "task_id": "objective-name-task",
            "task_type": "capture",
            "plate_type": "24-well",
            "objective_name": "10x",
            "capture": {"save_dir": "data/captures/objective-name-task"},
        },
        "objective": {"fov_mm": {"width": 1.2, "height": 0.8}},
        "camera": {
            "resolution": {"width": 1920, "height": 1200},
            "objective_settings": {"10x": {"exposure_us": 12000, "gain": 1.5}},
            "mvs_python_dir": "C:/MVS/Development/Samples/Python/MvImport",
        },
    }

    params = run_task.build_pipeline_params(ctx)

    assert params["objective_name"] == "10x"
    assert params["exposure_us"] == 12000
    assert params["gain"] == 1.5
    assert params["overlap"] == 0.1
    assert params["settle_s"] == 0.5
    assert params["motion"]["profile_vel"] == 500000
    assert params["motion"]["profile_acc"] == 500000
    assert params["motion"]["profile_dec"] == 500000


def test_build_pipeline_params_defaults_overlap_and_motion_profiles() -> None:
    from workflow import run_task

    ctx = {
        "task": {
            "task_id": "defaults-task",
            "task_type": "capture",
            "plate_type": "24-well",
            "objective_name": "4x",
            "capture": {"save_dir": "data/captures/defaults-task"},
        },
        "objective": {"fov_mm": {"width": 3.22, "height": 3.22}},
        "camera": {
            "resolution": {"width": 5120, "height": 5120},
            "mvs_python_dir": "/opt/MVS/Samples/64/Python/MvImport",
        },
    }

    params = run_task.build_pipeline_params(ctx)
    assert params["overlap"] == 0.1
    assert params["settle_s"] == 0.5
    assert params["motion"]["profile_vel"] == 500000
    assert params["motion"]["profile_acc"] == 500000
    assert params["motion"]["profile_dec"] == 500000


def test_build_pipeline_params_keeps_explicit_zero_overlap_and_profiles() -> None:
    from workflow import run_task

    ctx = {
        "task": {
            "task_id": "explicit-task",
            "task_type": "capture",
            "plate_type": "24-well",
            "objective_name": "4x",
            "capture": {"save_dir": "data/captures/explicit-task"},
            "scan": {"overlap": 0.0},
            "motion": {"profile_vel": 100000, "profile_acc": 200000, "profile_dec": 300000},
        },
        "objective": {"fov_mm": {"width": 3.22, "height": 3.22}},
        "camera": {
            "resolution": {"width": 5120, "height": 5120},
            "mvs_python_dir": "/opt/MVS/Samples/64/Python/MvImport",
        },
    }

    params = run_task.build_pipeline_params(ctx)
    assert params["overlap"] == 0.0
    assert params["motion"]["profile_vel"] == 100000
    assert params["motion"]["profile_acc"] == 200000
    assert params["motion"]["profile_dec"] == 300000


def test_objective_executor_accepts_objective_name() -> None:
    from workflow import objective_executor

    result = objective_executor.ensure_objective_for_task(
        {"task_id": "objective-name-task", "objective_name": "10x"},
        {"objectives": {"10x": {"switch": {"enabled": False}}}},
    )

    assert result["requested_objective"] == "10x"
    assert result["switched"] is False
    assert "10x 未启用 switch.enabled" in result["message"]


def _objective_switch_test_config(*, state: dict | None = None) -> dict:
    return {
        "objectives": {
            "10x": {
                "switch": {
                    "enabled": True,
                    "mode": "motor_manager",
                    "objective_target_pos": 332695,
                    "focus_target_pos": -2998604,
                    "focus_collision_limit_pos": -3229262,
                }
            }
        },
        "state": {"enabled": False} if state is None else state,
        "hardware": {
            "modbus": {"port": "COM3", "baudrate": 115200},
            "objective_axis": {"slave": 4},
            "focus_axis": {"slave": 3, "objective_switch_collision_limit_pos": -3168285},
        },
    }


@pytest.mark.parametrize("focus_position", [-3000000, -3168285])
def test_objective_executor_reads_focus_before_objective_then_focus_moves(
    monkeypatch, focus_position
) -> None:
    from workflow import objective_executor

    events = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeMotor:
        def __init__(self, client, slave):
            self.slave = slave

        def get_current_position(self):
            events.append(("read", self.slave))
            return focus_position

        def pp_absolute_move(self, *, target_pos, profile_vel, profile_acc, profile_dec):
            events.append(("move", self.slave, target_pos))
            return 0

    monkeypatch.setattr(objective_executor, "ModbusRTUClient", FakeClient)
    monkeypatch.setattr(objective_executor, "MotorManager", FakeMotor)

    result = objective_executor.ensure_objective_for_task(
        {"objective_name": "10x"},
        _objective_switch_test_config(),
    )

    assert events == [
        ("read", 3),
        ("move", 4, 332695),
        ("move", 3, -2998604),
    ]
    assert result["focus_position_before_switch"] == focus_position
    assert result["focus_move_result"]["target"] == -2998604
    assert result["objective_move_result"]["target"] == 332695


@pytest.mark.parametrize(
    ("focus_position", "read_raises", "error_pattern"),
    [
        (-3168286, False, "当前位置.*小于物镜切换碰撞安全限位"),
        (None, False, "读取电机3当前位置失败"),
        (None, True, "读取电机3当前位置失败"),
    ],
)
def test_objective_executor_focus_precheck_failure_prevents_all_motion_and_state_write(
    tmp_path, monkeypatch, focus_position, read_raises, error_pattern
) -> None:
    from workflow import objective_executor

    events = []
    state_file = tmp_path / "objective_state.json"

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeMotor:
        def __init__(self, client, slave):
            self.slave = slave

        def get_current_position(self):
            events.append(("read", self.slave))
            if read_raises:
                raise OSError("simulated Modbus read failure")
            return focus_position

        def pp_absolute_move(self, *, target_pos, profile_vel, profile_acc, profile_dec):
            events.append(("move", self.slave, target_pos))
            return 0

    monkeypatch.setattr(objective_executor, "ModbusRTUClient", FakeClient)
    monkeypatch.setattr(objective_executor, "MotorManager", FakeMotor)

    with pytest.raises(objective_executor.ObjectiveSwitchError, match=error_pattern):
        objective_executor.ensure_objective_for_task(
            {"objective_name": "10x"},
            _objective_switch_test_config(
                state={"enabled": True, "state_file": str(state_file), "assume_initial": None}
            ),
        )

    assert events == [("read", 3)]
    assert not state_file.exists()


def test_objective_executor_rejects_focus_target_beyond_collision_limit() -> None:
    from workflow import objective_executor

    with pytest.raises(objective_executor.ObjectiveSwitchError, match="碰撞安全限位"):
        objective_executor.ensure_objective_for_task(
            {"objective_name": "4x"},
            {
                "objectives": {
                    "4x": {
                        "switch": {
                            "enabled": True,
                            "objective_target_pos": 166347,
                            "focus_target_pos": -3500000,
                            "focus_collision_limit_pos": -3436433,
                        }
                    }
                },
                "state": {"enabled": False},
                "hardware": {"modbus": {"port": "COM3", "baudrate": 115200}},
            },
        )


def test_motor_manager_is_class_only_and_position_timeout_is_120_seconds() -> None:
    import inspect
    from importlib import import_module

    project_module = import_module("devices.motion.MotorManager")
    project_modbus_module = import_module("devices.motion.modbus")
    third_party_module = import_module("third_party.XWJJJ260511.MotorManager")
    third_party_modbus_module = import_module("third_party.XWJJJ260511.modbus")
    from third_party.XWJJJ260511.hardware.modbus_motor import ModbusFocusMotor

    for stale_name in (
        "point_home",
        "point_6",
        "point_12",
        "point_24",
        "point_48",
        "rpm_mm",
        "x4",
        "x10",
        "x4_focal",
        "x10_focal",
    ):
        assert not hasattr(project_module, stale_name)

    assert inspect.signature(project_module.MotorManager.pp_absolute_move).parameters[
        "timeout"
    ].default == 120.0
    assert inspect.signature(project_module.MotorManager.pp_relative_move).parameters[
        "timeout"
    ].default == 120.0
    assert inspect.signature(third_party_module.MotorManager.pp_absolute_move).parameters[
        "timeout"
    ].default == 120.0
    assert inspect.signature(ModbusFocusMotor).parameters["timeout"].default == 120.0
    assert inspect.signature(project_modbus_module.ModbusRTUClient.move_absolute_pp).parameters[
        "timeout"
    ].default == 120.0
    assert inspect.signature(third_party_modbus_module.ModbusRTUClient.move_absolute_pp).parameters[
        "timeout"
    ].default == 120.0


def test_motor_manager_get_current_position_is_one_pure_register_read() -> None:
    from devices.motion.modbus import ModbusRTUClient
    from devices.motion.MotorManager import MotorManager

    class ReadOnlyClient:
        def __init__(self):
            self.calls = []

        def _read_32bit(self, slave, register):
            self.calls.append(("read_32bit", slave, register))
            return -3000000

    client = ReadOnlyClient()
    manager = MotorManager(client, slave=3)

    assert manager.get_current_position() == -3000000
    assert client.calls == [("read_32bit", 3, ModbusRTUClient.REG_CURRENT_POS)]


def test_task_store_finalize_success_preserves_existing_timeline_and_wells() -> None:
    from workflow import task_store

    task = {
        "task_id": "finish-success",
        "task_type": "pipeline",
        "observe_scope": "well_list",
        "plate_type": "24-well",
        "objective": "10x",
        "target": {"well_list": ["A1", "A2"]},
        "capture": {"save_dir": "data/captures/finish-success"},
    }
    existing = task_store.build_accepted_record(task, None, True)
    existing.update(
        {
            "status": "running",
            "created_at": "created-at",
            "started_at": "started-at",
            "cancel_requested_at": "cancel-at",
        }
    )
    existing["wells"]["A2"]["operator_note"] = "keep me"
    result = {
        "status": "success",
        "task_id": "finish-success",
        "task_type": "pipeline",
        "observe_scope": "well_list",
        "plate_type": "24-well",
        "objective_name": "10x",
        "wells": [
            {
                "well_name": "A1",
                "capture_result_json": "data/captures/finish-success/A1/scan_result.json",
                "detect_result_json": "data/captures/finish-success/A1/detect_result.json",
            }
        ],
    }

    record = task_store.finalize_success_record(existing, task, result, None, True)

    assert record["status"] == "success"
    assert record["created_at"] == "created-at"
    assert record["started_at"] == "started-at"
    assert record["cancel_requested_at"] == "cancel-at"
    assert record["wells"]["A1"]["status"] == "success"
    assert record["wells"]["A2"]["operator_note"] == "keep me"
    assert record["wells"]["A2"]["image_dir"] == existing["wells"]["A2"]["image_dir"]


def test_task_store_finalize_failed_preserves_timeline_and_marks_active_wells() -> None:
    from workflow import task_store

    task = {
        "task_id": "finish-failed",
        "task_type": "capture",
        "observe_scope": "well_list",
        "plate_type": "24-well",
        "objective": "10x",
        "target": {"well_list": ["A1", "A2"]},
        "capture": {"save_dir": "data/captures/finish-failed"},
    }
    existing = task_store.build_accepted_record(task, None, True)
    existing.update(
        {
            "status": "running",
            "created_at": "created-at",
            "started_at": "started-at",
            "cancel_requested_at": "cancel-at",
        }
    )
    existing["wells"]["A1"]["status"] = "running"
    existing["wells"]["A2"]["status"] = "success"

    record = task_store.finalize_failed_record(existing, task, "camera failed", None, True)

    assert record["status"] == "failed"
    assert record["created_at"] == "created-at"
    assert record["started_at"] == "started-at"
    assert record["cancel_requested_at"] == "cancel-at"
    assert record["wells"]["A1"]["status"] == "failed"
    assert record["wells"]["A1"]["previous_status"] == "running"
    assert record["wells"]["A2"]["status"] == "success"
    assert record["error"] == "camera failed"


def test_api_server_get_task_result_uses_retry_json_reader(tmp_path, monkeypatch) -> None:
    from workflow import api_server, path_guard, task_store

    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path / "task_index"))
    monkeypatch.setattr(path_guard, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(path_guard, "OUTPUTS_ROOT", tmp_path / "outputs")
    result_path = tmp_path / "result.json"
    task_store.write_task_record(
        {
            "task_id": "result-task",
            "status": "success",
            "result_json_path": str(result_path),
            "updated_at": task_store.utc_now(),
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


def test_task_artifacts_active_result_returns_objective_name_only() -> None:
    from workflow.task_artifacts import build_task_result_response

    response = build_task_result_response(
        {
            "task_id": "active-objective",
            "status": "running",
            "objective_name": "10x",
            "progress": 20,
            "message": "running",
        },
        {"queued", "running"},
    )

    assert response["objective_name"] == "10x"
    assert "objective" not in response


def test_well_images_endpoint_paginates_and_hides_part_files(tmp_path, monkeypatch) -> None:
    from workflow import api_server, path_guard, task_store
    from workflow.task_artifacts import TaskArtifactError, resolve_well_image_file

    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path / "task_index"))
    monkeypatch.setattr(path_guard, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(path_guard, "OUTPUTS_ROOT", tmp_path / "outputs")
    image_dir = tmp_path / "captures" / "A1" / "images"
    image_dir.mkdir(parents=True)
    for name in ("001.bmp", "002.bmp", "003.png", "004.part.bmp", "recording.part.avi"):
        (image_dir / name).write_bytes(b"x")

    task_store.write_task_record(
        {
            "task_id": "images-task",
            "status": "success",
            "updated_at": task_store.utc_now(),
            "wells": {"A1": {"image_dir": str(image_dir)}},
        }
    )

    first_page = api_server.list_well_images("images-task", "A1", limit=2, offset=0)
    assert first_page["images"] == ["001.bmp", "002.bmp"]
    assert first_page["total"] == 3
    assert first_page["limit"] == 2
    assert first_page["offset"] == 0
    assert first_page["has_more"] is True

    second_page = api_server.list_well_images("images-task", "A1", page=2, page_size=2)
    assert second_page["images"] == ["003.png"]
    assert second_page["offset"] == 2
    assert second_page["has_more"] is False

    with pytest.raises(TaskArtifactError) as exc:
        resolve_well_image_file(task_store.read_task_record("images-task"), "A1", "004.part.bmp")
    assert exc.value.error_code == "INCOMPLETE_ARTIFACT_NOT_AVAILABLE"


def test_camera_executor_records_to_part_file_then_promotes(tmp_path) -> None:
    from workflow import camera_executor

    class FakeVideo:
        def __init__(self, saved_path: str) -> None:
            self.saved_path = saved_path
            self.width = 640
            self.height = 480
            self.pixel_type = 0
            self.frame_rate = 10.0
            self.bitrate_kbps = 1000
            self.frame_count = 1
            self.duration_s = 1.0
            self.timestamp_started = 1.0
            self.timestamp_finished = 2.0

    class FakeCamera:
        def __init__(self) -> None:
            self.requested_save_path = ""

        def record_video(self, *, save_path, duration_s, fps, bitrate_kbps, timeout_ms):
            self.requested_save_path = str(save_path)
            assert duration_s == 1.0
            part_path = Path(self.requested_save_path)
            assert part_path.parent == tmp_path
            assert part_path.name.endswith(".part.avi")
            part_path.write_bytes(b"video")
            return FakeVideo(str(part_path))

    final_path = tmp_path / "record.avi"
    result = camera_executor.record_video_with_opened_camera(
        cam=FakeCamera(),
        save_path=str(final_path),
        duration_s=1.0,
        fps=10.0,
        bitrate_kbps=1000,
    )

    assert result["saved_path"] == str(final_path)
    assert result["video"]["saved_path"] == str(final_path)
    assert final_path.read_bytes() == b"video"
    assert list(tmp_path.glob("*.part.avi")) == []

    explicit_part_input = tmp_path / "explicit.part.avi"
    explicit_final_path = tmp_path / "explicit.avi"
    explicit_result = camera_executor.record_video_with_opened_camera(
        cam=FakeCamera(),
        save_path=str(explicit_part_input),
        duration_s=1.0,
        fps=10.0,
        bitrate_kbps=1000,
    )

    assert explicit_result["saved_path"] == str(explicit_final_path)
    assert explicit_result["video"]["saved_path"] == str(explicit_final_path)
    assert explicit_final_path.read_bytes() == b"video"
    assert not explicit_part_input.exists()


def test_api_server_hardware_guard_allows_task_with_active_camera_record(monkeypatch) -> None:
    from workflow import hardware_guard

    hardware_guard.reset_hardware_owners()

    hardware_guard.acquire_hardware_operation("camera_record", "recording.avi")
    camera_owner = hardware_guard._CAMERA_RECORD_OWNER
    monkeypatch.setattr(hardware_guard, "_sync_camera_record_owner", lambda: None)
    monkeypatch.setattr(
        hardware_guard,
        "_camera_record_transition_snapshot",
        lambda: (camera_owner, "recording"),
    )
    try:
        hardware_guard.acquire_hardware_operation("task", "allowed-task")
        owners = hardware_guard.current_hardware_owners()
        assert {owner["kind"] for owner in owners} == {"camera_record", "task"}
        assert {owner["operation_id"] for owner in owners} == {"recording.avi", "allowed-task"}
    finally:
        hardware_guard.release_hardware_operation("task", "allowed-task")
        hardware_guard.release_hardware_operation("camera_record", "recording.avi")


def test_api_server_hardware_guard_releases_terminal_task_record(tmp_path, monkeypatch) -> None:
    from workflow import hardware_guard, task_store

    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path))
    hardware_guard.reset_hardware_owners()
    task_store.write_task_record(
        {
            "task_id": "finished-task",
            "status": "success",
            "updated_at": task_store.utc_now(),
        }
    )
    with hardware_guard._HARDWARE_OPERATION_LOCK:
        hardware_guard._HARDWARE_OWNER = {
            "kind": "task",
            "operation_id": "finished-task",
            "started_at": task_store.utc_now(),
            "sync_after_monotonic": 0.0,
        }

    assert hardware_guard.current_hardware_owner() is None


def test_api_server_startup_recovery_marks_active_tasks_interrupted(tmp_path, monkeypatch) -> None:
    from workflow import task_store

    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path))
    task_store.write_task_record(
        {
            "task_id": "running-task",
            "status": "running",
            "progress": 25,
            "updated_at": task_store.utc_now(),
            "wells": {
                "A1": {"status": "running", "message": "capturing"},
                "A2": {"status": "queued", "message": "waiting"},
                "A3": {"status": "success", "message": "completed"},
            },
        }
    )
    task_store.write_task_record(
        {
            "task_id": "success-task",
            "status": "success",
            "message": "task completed",
            "updated_at": task_store.utc_now(),
        }
    )

    stats = task_store.recover_interrupted_task_records()

    assert stats["interrupted"] == 1
    running = task_store.read_task_record("running-task")
    assert running["status"] == "interrupted"
    assert running["previous_status"] == "running"
    assert running["interrupted_reason"] == "service_restarted"
    assert running["progress"] == 25
    assert running["finished_at"]
    assert running["interrupted_at"]
    assert "API 服务启动" in running["message"]
    assert running["wells"]["A1"]["status"] == "interrupted"
    assert running["wells"]["A1"]["previous_status"] == "running"
    assert running["wells"]["A1"]["interrupted_reason"] == "service_restarted"
    assert running["wells"]["A2"]["status"] == "interrupted"
    assert running["wells"]["A2"]["interrupted_reason"] == "service_restarted"
    assert running["wells"]["A3"]["status"] == "success"

    success = task_store.read_task_record("success-task")
    assert success["status"] == "success"
    assert "previous_status" not in success


def test_api_server_request_paths_are_normalized_under_project_roots() -> None:
    from workflow import path_guard, task_runtime
    from workflow.api_models import ExecuteTaskRequest

    req = ExecuteTaskRequest(
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

    normalized = task_runtime.normalize_execute_task_request(req)

    assert normalized.camera_path == str((path_guard.CONFIG_ROOT / "camera.yaml").resolve(strict=False))
    assert normalized.objectives_path == str((path_guard.CONFIG_ROOT / "objectives.yaml").resolve(strict=False))
    assert normalized.plates_path == str((path_guard.CONFIG_ROOT / "plates.yaml").resolve(strict=False))
    assert normalized.dump_json == str((path_guard.DATA_ROOT / "captures" / "path-task" / "api_result.json").resolve(strict=False))
    assert normalized.task["capture"]["save_dir"] == str((path_guard.DATA_ROOT / "captures" / "path-task").resolve(strict=False))
    assert normalized.task["scan"]["output_json"] == str((path_guard.OUTPUTS_ROOT / "path-task" / "scan_result.json").resolve(strict=False))
    assert normalized.task["compensate"]["closed_loop"]["save_dir"] == str(
        (path_guard.DATA_ROOT / "captures" / "path-task" / "closed_loop").resolve(strict=False)
    )


def test_api_server_request_paths_reject_outside_project_roots() -> None:
    from workflow import path_guard, task_runtime
    from workflow.api_models import ExecuteTaskRequest

    with pytest.raises(path_guard.PathGuardError) as config_exc:
        task_runtime.normalize_execute_task_request(
            ExecuteTaskRequest(
                task={"task_id": "bad-config"},
                camera_path="C:/Windows/camera.yaml",
            )
        )
    assert config_exc.value.status_code == 400
    assert config_exc.value.error_code == "PATH_OUT_OF_ALLOWED_ROOT"

    with pytest.raises(path_guard.PathGuardError) as output_exc:
        task_runtime.normalize_execute_task_request(
            ExecuteTaskRequest(
                task={
                    "task_id": "bad-output",
                    "capture": {"save_dir": "../outside-captures"},
                }
            )
        )
    assert output_exc.value.status_code == 400
    assert output_exc.value.error_code == "PATH_OUT_OF_ALLOWED_ROOT"


def test_camera_record_service_config_error_returns_400() -> None:
    from workflow import camera_record_service, hardware_guard
    from workflow.api_models import CameraRecordStartRequest

    hardware_guard.reset_hardware_owners()

    def fail_load_settings(_req):
        raise ValueError("bad camera config")

    with pytest.raises(camera_record_service.CameraRecordServiceError) as exc:
        camera_record_service.start_camera_recording(
            CameraRecordStartRequest(save_path="data/camera_records/config-error.avi"),
            settings_loader=fail_load_settings,
        )

    assert exc.value.status_code == 400
    assert exc.value.error_code == "CAMERA_RECORD_CONFIG_INVALID"
    assert exc.value.message
    assert hardware_guard.current_hardware_owners() == []


def test_api_server_camera_record_request_rejects_invalid_ranges() -> None:
    from workflow.api_models import CameraRecordStartRequest

    with pytest.raises(ValidationError):
        CameraRecordStartRequest(fps=0)
    with pytest.raises(ValidationError):
        CameraRecordStartRequest(bitrate_kbps=0)
    with pytest.raises(ValidationError):
        CameraRecordStartRequest(timeout_ms=-1)
    with pytest.raises(ValidationError):
        CameraRecordStartRequest(device_index=-1)
    with pytest.raises(ValidationError):
        CameraRecordStartRequest(exposure_us=0)
    with pytest.raises(ValidationError):
        CameraRecordStartRequest(gain=-0.1)


def test_api_server_request_validation_error_returns_public_error() -> None:
    from workflow import api_errors

    class RequestStub:
        class UrlStub:
            path = "/api/camera/record/start"

        url = UrlStub()

    response = asyncio.run(
        api_errors.request_validation_exception_handler(
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
    from workflow import api_errors

    class RequestStub:
        class UrlStub:
            path = "/missing"

        url = UrlStub()

    response = asyncio.run(
        api_errors.http_exception_handler(
            RequestStub(),
            HTTPException(status_code=404, detail="C:/secret/config.yaml"),
        )
    )

    assert response.status_code == 404
    assert json.loads(response.body)["detail"] == {
        "error_code": "HTTP_404",
        "message": "请求的资源不存在",
    }


def test_api_server_stage_reciprocation_request_rejects_invalid_ranges() -> None:
    from workflow.api_models import StageReciprocationStartRequest

    with pytest.raises(ValidationError):
        StageReciprocationStartRequest(profile_vel=0)
    with pytest.raises(ValidationError):
        StageReciprocationStartRequest(profile_acc=0)
    with pytest.raises(ValidationError):
        StageReciprocationStartRequest(profile_dec=0)
    with pytest.raises(ValidationError):
        StageReciprocationStartRequest(poll_s=0)
    with pytest.raises(ValidationError):
        StageReciprocationStartRequest(move_timeout_s=0)
    with pytest.raises(ValidationError):
        StageReciprocationStartRequest(max_cycles=0)


def test_stage_reciprocation_request_schema_removes_and_forbids_legacy_fields() -> None:
    from workflow.api_models import StageReciprocationStartRequest

    legacy_fields = {
        "point_a_x",
        "point_a_y",
        "point_b_x",
        "point_b_y",
        "limit_check_enabled",
        "x_min",
        "x_max",
        "y_min",
        "y_max",
        "safety_margin",
    }
    schema = StageReciprocationStartRequest.model_json_schema()

    assert legacy_fields.isdisjoint(StageReciprocationStartRequest.model_fields)
    assert legacy_fields.isdisjoint(schema["properties"])
    assert schema["additionalProperties"] is False
    for field in legacy_fields:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            StageReciprocationStartRequest.model_validate({field: 0})


@pytest.mark.parametrize(
    "legacy_field",
    [
        "point_a_x",
        "point_a_y",
        "point_b_x",
        "point_b_y",
        "limit_check_enabled",
        "x_min",
        "x_max",
        "y_min",
        "y_max",
        "safety_margin",
    ],
)
def test_stage_reciprocation_api_rejects_legacy_fields_before_hardware_use(
    monkeypatch, legacy_field
) -> None:
    from workflow import api_server, stage_reciprocation

    hardware_calls = []
    monkeypatch.setattr(
        api_server,
        "acquire_hardware_operation",
        lambda *_args, **_kwargs: hardware_calls.append("lock"),
    )
    monkeypatch.setattr(
        stage_reciprocation.stage_reciprocation_controller,
        "start",
        lambda *_args, **_kwargs: hardware_calls.append("thread"),
    )

    async def invoke_app() -> list[dict]:
        body = json.dumps({legacy_field: 0}).encode("utf-8")
        messages = []
        request_received = False

        async def receive():
            nonlocal request_received
            if not request_received:
                request_received = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        await api_server.app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/stage/reciprocation/start",
                "raw_path": b"/api/stage/reciprocation/start",
                "query_string": b"",
                "root_path": "",
                "headers": [(b"content-type", b"application/json")],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
            },
            receive,
            send,
        )
        return messages

    messages = asyncio.run(invoke_app())

    response_start = next(message for message in messages if message["type"] == "http.response.start")
    assert response_start["status"] == 422
    assert hardware_calls == []


def test_task_runtime_cancel_request_marks_record_and_sets_event(tmp_path, monkeypatch) -> None:
    from workflow import task_runtime, task_store

    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path))
    cancel_event = threading.Event()
    with task_runtime._TASK_CANCEL_LOCK:
        task_runtime._TASK_CANCEL_EVENTS.clear()
    task_runtime.register_task_cancel_event("cancel-me", cancel_event)
    task_store.write_task_record(
        {
            "task_id": "cancel-me",
            "status": "running",
            "progress": 40,
            "updated_at": task_store.utc_now(),
        }
    )

    result = task_runtime.cancel_task_request("cancel-me")

    assert result["status"] == "cancel_requested"
    assert result["cancel_requested"] is True
    assert cancel_event.is_set()
    record = task_store.read_task_record("cancel-me")
    assert record["status"] == "running"
    assert record["cancel_requested"] is True
    assert record["cancel_requested_at"]

    task_runtime.unregister_task_cancel_event("cancel-me")


def test_task_runtime_run_task_async_writes_canceled_record(tmp_path, monkeypatch) -> None:
    from workflow import task_runtime, task_store
    from workflow.api_models import ExecuteTaskRequest
    from workflow.task_control import TaskCanceled

    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path))
    task = {"task_id": "worker-cancel", "task_type": "capture"}
    req = ExecuteTaskRequest(task=task)
    task_store.write_task_record(task_store.build_accepted_record(task, None, True))

    def fake_execute_task_request(*_args, **_kwargs):
        raise TaskCanceled("operator canceled")

    task_runtime.run_task_async(task, req, threading.Event(), task_executor=fake_execute_task_request)

    record = task_store.read_task_record("worker-cancel")
    assert record["status"] == "canceled"
    assert record["cancel_requested"] is True
    assert record["canceled_at"]
    assert record["cancel_reason"] == "operator canceled"


def test_task_runtime_progress_callback_updates_task_and_well_record(tmp_path, monkeypatch) -> None:
    from workflow import task_runtime, task_store

    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path))
    task = {
        "task_id": "progress-task",
        "task_type": "pipeline",
        "observe_scope": "single_well",
        "plate_type": "24-well",
        "objective": "10x",
        "target": {"well_name": "A1"},
        "capture": {"save_dir": "data/captures/progress-task"},
    }
    record = task_store.build_accepted_record(task, None, True)
    record["status"] = "running"
    task_store.write_task_record(record)

    callback = task_runtime.make_task_progress_callback("progress-task")
    callback("detect", 42, "A1", "detecting image 1/2")

    updated = task_store.read_task_record("progress-task")
    assert updated["progress"] == 42
    assert updated["progress_source"] == "executor"
    assert updated["current_stage"] == "detect"
    assert updated["current_well"] == "A1"
    assert updated["message"] == "detecting image 1/2"
    assert updated["wells"]["A1"]["status"] == "running"
    assert updated["wells"]["A1"]["progress"] == 42
    assert updated["wells"]["A1"]["current_stage"] == "detect"


def test_task_runtime_manager_runs_tasks_with_single_worker_queue(tmp_path, monkeypatch) -> None:
    from workflow import hardware_guard, task_runtime
    from workflow.api_models import ExecuteTaskRequest

    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path))
    hardware_guard.reset_hardware_owners()
    manager = task_runtime.TaskRuntimeManager(maxsize=4)
    seen = []

    def fake_executor(*, raw_task_cfg, **_kwargs):
        task_id = raw_task_cfg["task"]["task_id"]
        seen.append(task_id)
        return {"status": "success", "task_id": task_id, "task_type": "capture"}

    manager.start()
    try:
        first = manager.submit(ExecuteTaskRequest(task={"task_id": "queue-1", "task_type": "capture"}), task_executor=fake_executor)
        second = manager.submit(ExecuteTaskRequest(task={"task_id": "queue-2", "task_type": "capture"}), task_executor=fake_executor)
        manager._queue.join()
    finally:
        manager.stop(timeout_s=2)
        hardware_guard.reset_hardware_owners()

    assert first["status"] == "accepted"
    assert second["status"] == "accepted"
    assert seen == ["queue-1", "queue-2"]


def test_task_runtime_manager_rejects_when_queue_is_full(tmp_path, monkeypatch) -> None:
    from workflow import hardware_guard, task_runtime
    from workflow.api_models import ExecuteTaskRequest

    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path))
    hardware_guard.reset_hardware_owners()
    manager = task_runtime.TaskRuntimeManager(maxsize=1)
    started = threading.Event()
    release = threading.Event()

    def blocking_executor(*, raw_task_cfg, **_kwargs):
        started.set()
        release.wait(timeout=5)
        task_id = raw_task_cfg["task"]["task_id"]
        return {"status": "success", "task_id": task_id, "task_type": "capture"}

    manager.start()
    try:
        manager.submit(ExecuteTaskRequest(task={"task_id": "queue-full-1", "task_type": "capture"}), task_executor=blocking_executor)
        assert started.wait(timeout=2)
        manager.submit(ExecuteTaskRequest(task={"task_id": "queue-full-2", "task_type": "capture"}), task_executor=blocking_executor)
        with pytest.raises(task_runtime.TaskRuntimeError) as exc:
            manager.submit(
                ExecuteTaskRequest(task={"task_id": "queue-full-3", "task_type": "capture"}),
                task_executor=blocking_executor,
            )
        assert exc.value.status_code == 429
        assert exc.value.error_code == "TASK_QUEUE_FULL"
    finally:
        release.set()
        manager._queue.join()
        manager.stop(timeout_s=2)
        hardware_guard.reset_hardware_owners()


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


def test_scan_executor_reports_real_progress_events(monkeypatch) -> None:
    from workflow import scan_executor

    events = []

    def fake_move_to_absolute(**kwargs):
        return {
            "target": {"x": kwargs["x_target"], "y": kwargs["y_target"]},
            "before": {"x": {"current_pos": 0}, "y": {"current_pos": 0}},
            "after": {
                "x": {"current_pos": kwargs["x_target"]},
                "y": {"current_pos": kwargs["y_target"]},
            },
            "err_to_target": {"x": 0, "y": 0},
        }

    monkeypatch.setattr(scan_executor, "move_to_absolute", fake_move_to_absolute)
    monkeypatch.setattr(
        scan_executor,
        "capture_with_opened_camera",
        lambda **_kwargs: {"saved_path": "data/captures/progress/A1/images/image_1.bmp"},
    )

    params = {
        "task_id": "scan-progress",
        "task_type": "capture",
        "plate_type": "24-well",
        "well_name": "A1",
        "objective_name": "4x",
        "motion": {"profile_vel": 1, "profile_acc": 1, "profile_dec": 1},
        "settle_s": 0,
        "save_dir": "data/captures/progress/A1/images",
        "filename_pattern": "image_{index}.bmp",
        "_progress_callback": lambda stage, progress, well, message: events.append((stage, progress, well, message)),
    }
    plan = {
        "points": [
            {
                "index": 1,
                "row_index": 0,
                "col_index": 0,
                "view_down_mm": 0.0,
                "view_right_mm": 0.0,
                "stage_x_target": 10,
                "stage_y_target": 20,
            }
        ],
        "reference": {},
        "scan_config": {},
    }

    scan_executor.execute_scan_capture({"plate": {}}, params, plan, cam=object())

    assert events[0] == ("capture", 0, "A1", "capture started")
    assert any(event[3] == "moving to scan point 1/1" for event in events)
    assert events[-1] == ("capture", 100, "A1", "captured scan point 1/1")


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


def test_detect_executor_reports_real_progress_events(tmp_path, monkeypatch) -> None:
    from PIL import Image
    from workflow import detect_executor

    image_path = tmp_path / "image_1.bmp"
    Image.new("L", (8, 6), color=0).save(image_path)
    events = []

    monkeypatch.setattr(
        detect_executor,
        "run_detect_on_image",
        lambda *_args, **_kwargs: {"clone_count": 0, "clones": []},
    )

    scan_result = {
        "scan_config": {"fov_mm": {"width": 1.0, "height": 1.0}},
        "captures": [
            {
                "index": 1,
                "row_index": 0,
                "col_index": 0,
                "stage_x_target": 10,
                "stage_y_target": 20,
                "motion_result": {
                    "after": {"x": {"current_pos": 10}, "y": {"current_pos": 20}},
                },
                "capture_result": {"saved_path": str(image_path)},
            }
        ],
    }
    params = {
        "task_id": "detect-progress",
        "plate_type": "24-well",
        "well_name": "A1",
        "objective_name": "4x",
        "_progress_callback": lambda stage, progress, well, message: events.append((stage, progress, well, message)),
    }

    detect_executor.execute_detect_on_scan_result({"task": {"detect": {"save_overlay": False}}}, params, scan_result)

    assert events[0] == ("detect", 0, "A1", "detect started")
    assert any(event[3] == "detecting image 1/1" for event in events)
    assert events[-1] == ("detect", 100, "A1", "detect completed")


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


def test_compensate_executor_reports_real_progress_events(monkeypatch) -> None:
    from workflow import compensate_executor

    events = []
    monkeypatch.setattr(
        compensate_executor,
        "_move_to_compensate_target",
        lambda **_kwargs: {
            "after": {"x": {"current_pos": 100}, "y": {"current_pos": 200}},
        },
    )

    ctx = {
        "plate": {
            "pulses_per_mm": 1,
            "x_stage_sign_for_view_right": 1,
            "y_stage_sign_for_view_down": 1,
        }
    }
    params = {
        "task_id": "compensate-progress",
        "plate_type": "24-well",
        "well_name": "A1",
        "objective_name": "4x",
        "motion": {"profile_vel": 1, "profile_acc": 1, "profile_dec": 1},
        "_progress_callback": lambda stage, progress, well, message: events.append((stage, progress, well, message)),
    }
    detect_result = {
        "images": [
            {
                "index": 1,
                "stage_x_actual": 100,
                "stage_y_actual": 200,
                "mm_per_pixel": {"x": 0.1, "y": 0.1},
                "clones": [
                    {
                        "clone_id": "clone-1",
                        "offset_from_image_center_px": [1, 1],
                        "is_pickable": True,
                    }
                ],
            }
        ]
    }

    compensate_executor.execute_compensate_on_detect_result(ctx, params, detect_result)

    assert events[0] == ("compensate", 5, "A1", "selecting clone for compensation")
    assert any(event[3] == "moving to compensate target" for event in events)
    assert events[-1] == ("compensate", 100, "A1", "compensate completed")


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
    monkeypatch.setattr(stage_executor.time, "sleep", lambda _seconds: None)
    FakeClient.events = []

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
    controlwords = [event for event in FakeClient.events if event[0] == "controlword"]
    assert controlwords[:4] == [
        ("controlword", 1, 0x0F),
        ("controlword", 2, 0x0F),
        ("controlword", 1, 0x1F),
        ("controlword", 2, 0x1F),
    ]
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
    monkeypatch.setattr(stage_executor.time, "sleep", lambda _seconds: None)

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


def test_stage_executor_quick_stops_xy_when_y_pp_clear_write_fails(monkeypatch) -> None:
    from workflow import stage_executor

    class FakeClient:
        last_instance = None
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
            if reg == self.REG_TARGET_POS:
                self.targets[slave] = int(value)
            return True

        def _write_controlword(self, slave, value):
            if slave == 2 and int(value) == self.CMD_ENABLE_OPERATION:
                return False
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
    monkeypatch.setattr(stage_executor.time, "sleep", lambda _seconds: None)

    with pytest.raises(
        stage_executor.StageMotionError,
        match=r"y axis failed to clear PP trigger bit \(controlword=0x0F, slave=2\)",
    ):
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


def test_compute_well_start_maps_columns_to_x_and_rows_to_y() -> None:
    plate_cfg = {
        "rows": 4,
        "cols": 6,
        "a1_start": {"x": 8865800, "y": 6185500},
        "well_diameter_mm": 13.7,
        "well_gap_mm": 3.5,
        "well_step": {"col": {"x": -2537000, "y": 0}, "row": {"x": 0, "y": -2537000}},
        "pulses_per_mm": 147500,
    }

    assert compute_well_start(plate_cfg, "A1") == {
        "x": 8865800,
        "y": 6185500,
        "row_index": 0,
        "col_index": 0,
        "well_name": "A1",
    }
    assert compute_well_start(plate_cfg, "A2") == {
        "x": 6328800,
        "y": 6185500,
        "row_index": 0,
        "col_index": 1,
        "well_name": "A2",
    }
    assert compute_well_start(plate_cfg, "B1") == {
        "x": 8865800,
        "y": 3648500,
        "row_index": 1,
        "col_index": 0,
        "well_name": "B1",
    }
    assert compute_well_start(plate_cfg, "B2") == {
        "x": 6328800,
        "y": 3648500,
        "row_index": 1,
        "col_index": 1,
        "well_name": "B2",
    }


def test_compute_well_start_uses_independent_axis_pulses_per_mm() -> None:
    plate_cfg = {
        "rows": 2,
        "cols": 2,
        "a1_start": {"x": 1000, "y": 2000},
        "well_diameter_mm": 1.0,
        "well_gap_mm": 0.0,
        "well_step": {"col": {"x": -100, "y": 0}, "row": {"x": 0, "y": -200}},
        "pulses_per_mm": {"x": 100, "y": 200},
    }

    assert compute_well_start(plate_cfg, "B2") == {
        "x": 900,
        "y": 1800,
        "row_index": 1,
        "col_index": 1,
        "well_name": "B2",
    }


def test_scan_planner_maps_view_right_to_x_and_view_down_to_y() -> None:
    from workflow.scan_planner import plan_single_well_scan

    plate_cfg = {
        "rows": 1,
        "cols": 1,
        "a1_start": {"x": 1000, "y": 2000},
        "well_diameter_mm": 2.0,
        "well_gap_mm": 0.0,
        "well_step": {"col": {"x": 100, "y": 0}, "row": {"x": 0, "y": 200}},
        "pulses_per_mm": {"x": 100, "y": 200},
        "x_stage_sign_for_view_right": 1,
        "y_stage_sign_for_view_down": 1,
        "stage_limits": {"enabled": False},
    }
    params = {
        "task_id": "axis-standard",
        "task_type": "capture",
        "plate_type": "test-plate",
        "well_name": "A1",
        "objective_name": "4x",
        "fov_mm": {"width": 1.0, "height": 1.0},
        "overlap": 0.0,
    }

    plan = plan_single_well_scan({"plate": plate_cfg}, params)

    for point in plan["points"]:
        assert point["stage_x_target"] == round(1000 + point["view_right_mm"] * 100)
        assert point["stage_y_target"] == round(2000 + point["view_down_mm"] * 200)
    assert plan["reference"]["pulses_per_mm"] == {"x": 100.0, "y": 200.0}
    assert plan["reference"]["x_stage_sign_for_view_right"] == 1
    assert plan["reference"]["y_stage_sign_for_view_down"] == 1


def test_compensate_maps_horizontal_offset_to_x_and_vertical_offset_to_y() -> None:
    from workflow.compensate_executor import _calc_compensate_target

    result = _calc_compensate_target(
        ctx={
            "plate": {
                "pulses_per_mm": {"x": 100, "y": 200},
                "x_stage_sign_for_view_right": 1,
                "y_stage_sign_for_view_down": 1,
            }
        },
        params={"compensate_scale": {"x": 1.0, "y": 1.0}},
        image_item={
            "stage_x_actual": 1000,
            "stage_y_actual": 2000,
            "mm_per_pixel": {"x": 0.1, "y": 0.2},
        },
        clone_item={"offset_from_image_center_px": [10, 20]},
    )

    assert result["offset_mm"] == {"view_right_mm": 1.0, "view_down_mm": 4.0}
    assert result["compensate_target"] == {"x": 900, "y": 1200}


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


def _write_stage_reciprocation_plates_config(tmp_path, mutate_plate=None) -> Path:
    config = load_yaml_unique("config/plates.yaml")
    if mutate_plate is not None:
        mutate_plate(config["plates"]["24-well"])
    plates_path = tmp_path / "plates.yaml"
    plates_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return plates_path


def test_stage_reciprocation_normalize_cfg_builds_fixed_24_well_targets(tmp_path) -> None:
    plates_path = _write_stage_reciprocation_plates_config(tmp_path)
    expected_limits = {
        "enabled": True,
        "x_min": -1295041,
        "x_max": 6525977,
        "y_min": -1095614,
        "y_max": 9284715,
        "safety_margin": 131072,
    }

    cfg = StageReciprocationController()._normalize_cfg(
        {
            "plates_path": str(plates_path),
            "limit_check_enabled": False,
            "x_min": 0,
            "x_max": 1,
            "y_min": 0,
            "y_max": 1,
            "safety_margin": 0,
            "max_cycles": 3,
        }
    )

    assert cfg["plate_type"] == "24-well"
    assert cfg["scan_wells"] == ["B2", "B3", "B4", "C2", "C3", "C4"]
    assert cfg["max_cycles"] == 3
    assert cfg["limits"] == expected_limits
    assert cfg["targets"][0] == {
        "index": 1,
        "well_name": "B2",
        "x": 4840541,
        "y": 5995754,
    }
    assert cfg["targets"][1] == {
        "index": 2,
        "well_name": "B3",
        "x": 3574035,
        "y": 5995754,
    }
    assert cfg["targets"][-1] == {
        "index": 6,
        "well_name": "C4",
        "x": 2304196,
        "y": 3482936,
    }
    x_safe = (
        expected_limits["x_min"] + expected_limits["safety_margin"],
        expected_limits["x_max"] - expected_limits["safety_margin"],
    )
    y_safe = (
        expected_limits["y_min"] + expected_limits["safety_margin"],
        expected_limits["y_max"] - expected_limits["safety_margin"],
    )
    for target in cfg["targets"]:
        assert x_safe[0] <= target["x"] <= x_safe[1]
        assert y_safe[0] <= target["y"] <= y_safe[1]


def test_stage_reciprocation_rejects_missing_stage_limits_before_thread_start(tmp_path) -> None:
    plates_path = _write_stage_reciprocation_plates_config(
        tmp_path,
        lambda plate: plate.pop("stage_limits"),
    )
    controller = StageReciprocationController()
    with pytest.raises(StageReciprocationError, match="stage_limits.*required mapping"):
        controller.start({"plates_path": str(plates_path)})
    assert controller._thread is None


def test_stage_reciprocation_rejects_disabled_stage_limits_before_thread_start(tmp_path) -> None:
    def disable_limits(plate):
        plate["stage_limits"]["enabled"] = False

    plates_path = _write_stage_reciprocation_plates_config(tmp_path, disable_limits)
    controller = StageReciprocationController()
    with pytest.raises(StageReciprocationError, match="stage limits are disabled"):
        controller.start({"plates_path": str(plates_path)})
    assert controller._thread is None


@pytest.mark.parametrize(
    ("mutate_limits", "error_pattern"),
    [
        (lambda limits: limits.update(x_min=limits["x_max"]), "x_min must be smaller"),
        (lambda limits: limits.update(safety_margin=10_000_000), "leaves no valid X travel range"),
    ],
)
def test_stage_reciprocation_rejects_invalid_plate_limits(
    tmp_path, mutate_limits, error_pattern
) -> None:
    def mutate_plate(plate):
        mutate_limits(plate["stage_limits"])

    plates_path = _write_stage_reciprocation_plates_config(tmp_path, mutate_plate)
    controller = StageReciprocationController()
    with pytest.raises(StageReciprocationError, match=error_pattern):
        controller.start({"plates_path": str(plates_path)})
    assert controller._thread is None


def test_stage_reciprocation_rejects_computed_target_outside_config_limits(monkeypatch) -> None:
    from workflow import stage_reciprocation

    plates_config = load_yaml_unique("config/plates.yaml")

    def fake_compute_well_start(plate, well_name):
        position = compute_well_start(plate, well_name)
        if well_name == "B2":
            position["x"] = 999_999_999
        return position

    monkeypatch.setattr(stage_reciprocation, "validate_plates_file", lambda _path: plates_config)
    monkeypatch.setattr(stage_reciprocation, "compute_well_start", fake_compute_well_start)

    controller = StageReciprocationController()
    with pytest.raises(StageReciprocationError, match=r"B2\.x=.*outside safe range"):
        controller.start({})
    assert controller._thread is None


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
        plate_cfg={
            "stage_limits": {
                "enabled": True,
                "x_min": 0,
                "x_max": 1000,
                "y_min": 0,
                "y_max": 1000,
                "safety_margin": 10,
            }
        },
    )

    assert result["status"] == "success"
    assert len(calls) == 1
    assert calls[0]["port"] == "COM3"
    assert calls[0]["x_target"] == 100
    assert calls[0]["y_target"] == 200
    assert calls[0]["arrival_tolerance_pulse"] == 3
    assert calls[0]["poll_s"] == 0.01
    assert calls[0]["stage_limits"] == {
        "enabled": True,
        "x_min": 0,
        "x_max": 1000,
        "y_min": 0,
        "y_max": 1000,
        "safety_margin": 10,
    }
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


def test_project_xy_stage_calibration_values() -> None:
    plates = load_yaml_unique("config/plates.yaml")["plates"]
    expected_a1 = {
        "6-well": (6198780, 7287429),
        "12-well": (5774368, 8116360),
        "24-well": (6110380, 8508572),
        "48-well": (5848352, 8942329),
    }
    expected_farthest = {
        "6-well": (1064474, 2220865, "B3"),
        "12-well": (654283, 1372092, "C4"),
        "24-well": (-232149, 970118, "D6"),
        "48-well": (-155568, 468175, "F8"),
    }
    expected_step = {
        "6-well": {"col": {"x": -2567153, "y": -3293}, "row": {"x": 0, "y": -5059978}},
        "12-well": {"col": {"x": -1706695, "y": 0}, "row": {"x": 0, "y": -3372134}},
        "24-well": {"col": {"x": -1266506, "y": 0}, "row": {"x": -3333, "y": -2512818}},
        "48-well": {"col": {"x": -858560, "y": 3}, "row": {"x": 1200, "y": -1694835}},
    }
    expected_teach = {
        "6-well": {
            "row_end": {"well": "A3", "x": 1064474, "y": 7280843},
            "col_end": {"well": "B1", "x": 6198780, "y": 2227451},
            "diagonal": {"well": "B3", "x": 1064507, "y": 2227451},
        },
        "12-well": {
            "row_end": {"well": "A4", "x": 654283, "y": 8116360},
            "col_end": {"well": "C1", "x": 5774368, "y": 1372091},
            "diagonal": {"well": "C4", "x": 654283, "y": 1372091},
        },
        "24-well": {
            "row_end": {"well": "A6", "x": -222148, "y": 8508572},
            "col_end": {"well": "D1", "x": 6100380, "y": 970117},
            "diagonal": {"well": "D6", "x": -226076, "y": 970117},
        },
        "48-well": {
            "row_end": {"well": "A8", "x": -161565, "y": 8942352},
            "col_end": {"well": "F1", "x": 5854353, "y": 468155},
            "diagonal": {"well": "F8", "x": -166565, "y": 499227},
        },
    }
    expected_inner_d = {
        "6-well": 34.26382324,
        "12-well": 21.44888184,
        "24-well": 15.56875488,
        "48-well": 10.28811523,
    }
    limits = {
        "enabled": True,
        "x_min": -1295041,
        "x_max": 6525977,
        "y_min": -1095614,
        "y_max": 9284715,
        "safety_margin": 131072,
    }

    for plate_type, plate in plates.items():
        assert (plate["a1_start"]["x"], plate["a1_start"]["y"]) == expected_a1[plate_type]
        assert plate["well_step"] == expected_step[plate_type]
        assert plate["well_diameter_mm"] == expected_inner_d[plate_type]
        assert plate["pulses_per_mm"] == {"x": 65536, "y": 131072}
        assert plate["stage_limits"] == limits
        assert "row_stage_sign" not in plate
        assert "col_stage_sign" not in plate
        assert plate["x_stage_sign_for_view_right"] == -1
        assert plate["y_stage_sign_for_view_down"] == -1
        assert plate["well_teach"] == expected_teach[plate_type]

        far_x, far_y, far_well = expected_farthest[plate_type]
        far_start = compute_well_start(plate, far_well)
        assert (far_start["x"], far_start["y"]) == (far_x, far_y)

        x_safe = (limits["x_min"] + limits["safety_margin"], limits["x_max"] - limits["safety_margin"])
        y_safe = (limits["y_min"] + limits["safety_margin"], limits["y_max"] - limits["safety_margin"])
        for x, y in (expected_a1[plate_type], (far_x, far_y)):
            assert x_safe[0] <= x <= x_safe[1]
            assert y_safe[0] <= y <= y_safe[1]


def test_project_handoff_point_matches_xy_calibration() -> None:
    point = load_yaml_unique("config/handoff.yaml")["handoff"]["points"]["robot_exchange"]
    assert (point["x"], point["y"]) == (5000000, 0)


def test_plates_validator_rejects_incomplete_axis_pulses_per_mm() -> None:
    cfg = load_yaml_unique("config/plates.yaml")
    cfg["plates"]["24-well"]["pulses_per_mm"].pop("y")

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_plates_config(cfg)

    assert "plates.24-well.pulses_per_mm.y" in str(exc_info.value)


def test_project_camera_config_passes_machine_validation(tmp_path) -> None:
    camera = load_yaml_unique("config/camera.yaml")
    camera["camera"]["mvs_python_dir"] = str(_fake_mvs_sdk_dir(tmp_path))
    objective_names = set(load_yaml_unique("config/objectives.yaml")["objectives"])
    validate_camera_config(
        camera,
        require_top_level=True,
        objective_names=objective_names,
    )


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
    assert "stopping" in str(request["error"])

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

    from devices import camera_controller as camera_controller_module
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
        cam._record_one_frame_unlocked(
            timeout_ms=camera_controller_module.RECORD_GRAB_TRANSFER_SLACK_MS
        )

    assert cam.input_calls == 0


def test_camera_controller_record_frame_accepts_valid_mono8_before_input() -> None:
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")

    import ctypes

    from devices import camera_controller as camera_controller_module
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
    frame = cam._record_one_frame_unlocked(
        timeout_ms=camera_controller_module.RECORD_GRAB_TRANSFER_SLACK_MS
    )

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


def test_project_autofocus_config_passes_machine_validation(tmp_path) -> None:
    sdk_dir = str(_fake_mvs_sdk_dir(tmp_path))
    autofocus = load_yaml_unique("config/autofocus.yaml")
    camera = load_yaml_unique("config/camera.yaml")
    objectives = load_yaml_unique("config/objectives.yaml")
    autofocus["camera"]["mvs_python_dir"] = sdk_dir
    camera["camera"]["mvs_python_dir"] = sdk_dir
    validate_autofocus_config(
        autofocus,
        objectives_cfg=objectives,
        camera_cfg=camera,
    )


def test_project_camera_identity_is_synced_for_autofocus() -> None:
    project_camera = load_yaml_unique("config/camera.yaml")["camera"]
    autofocus_camera = load_yaml_unique("config/autofocus.yaml")["camera"]

    assert project_camera["ip"] == autofocus_camera["ip"] == "192.168.1.253"
    assert project_camera["serial_number"] == autofocus_camera["serial_number"] == "DA8583237"


def test_autofocus_validator_rejects_camera_serial_mismatch() -> None:
    autofocus = load_yaml_unique("config/autofocus.yaml")
    objectives = load_yaml_unique("config/objectives.yaml")
    camera = load_yaml_unique("config/camera.yaml")
    autofocus["camera"]["serial_number"] = "WRONG-SERIAL"

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_autofocus_config(autofocus, objectives_cfg=objectives, camera_cfg=camera)

    assert "autofocus.camera.serial_number" in str(exc_info.value)
    assert "DA8583237" in str(exc_info.value)


def test_third_party_autofocus_passes_camera_identity(monkeypatch) -> None:
    from third_party.XWJJJ260511 import run as autofocus_run
    from third_party.XWJJJ260511.hardware import hikrobot_camera

    received = {}

    class FakeCamera:
        def __init__(self, **kwargs):
            received.update(kwargs)

    monkeypatch.setattr(hikrobot_camera, "HikrobotCamera", FakeCamera)
    autofocus_run._create_camera(
        {
            "camera": {
                "backend": "mvs",
                "serial_number": "DA8583237",
                "ip": "192.168.0.66",
                "net_export_ip": "192.168.0.10",
            },
            "motor": {"objective": "4x"},
        }
    )

    assert received["serial_number"] == "DA8583237"
    assert received["device"] == "192.168.0.66"


def test_third_party_camera_reads_gige_serial_number() -> None:
    from types import SimpleNamespace

    from third_party.XWJJJ260511.hardware.hikrobot_camera import HikrobotCamera

    mvs = SimpleNamespace(MV_GIGE_DEVICE=1, MV_GENTL_GIGE_DEVICE=2, MV_USB_DEVICE=4)
    device_info = SimpleNamespace(
        nTLayerType=1,
        SpecialInfo=SimpleNamespace(
            stGigEInfo=SimpleNamespace(chSerialNumber=b"DA8583237\x00\x00")
        ),
    )

    assert HikrobotCamera._get_mvs_device_serial(mvs, device_info) == "DA8583237"


def test_project_objective_focus_calibration_values() -> None:
    objectives_config = load_yaml_unique("config/objectives.yaml")
    objectives = objectives_config["objectives"]
    ranges = load_yaml_unique("config/autofocus.yaml")["motor"]["objective_ranges"]

    assert objectives["4x"]["switch"]["objective_target_pos"] == 166347
    assert objectives["4x"]["switch"]["focus_target_pos"] == -3002685
    assert objectives["4x"]["switch"]["focus_collision_limit_pos"] == -3436433
    assert ranges["4x"] == {"min_pos": -3082685, "max_pos": -2922685}

    assert objectives["10x"]["switch"]["objective_target_pos"] == 332695
    assert objectives["10x"]["switch"]["focus_target_pos"] == -2998604
    assert objectives["10x"]["switch"]["focus_collision_limit_pos"] == -3229262
    assert ranges["10x"] == {"min_pos": -3078604, "max_pos": -2918604}
    assert objectives_config["hardware"]["focus_axis"]["objective_switch_collision_limit_pos"] == -3168285


def test_autofocus_range_cannot_cross_objective_switch_collision_limit() -> None:
    autofocus = load_yaml_unique("config/autofocus.yaml")
    objectives = load_yaml_unique("config/objectives.yaml")
    camera = load_yaml_unique("config/camera.yaml")
    autofocus["motor"]["objective_ranges"]["4x"]["min_pos"] = -3168286

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_autofocus_config(autofocus, objectives_cfg=objectives, camera_cfg=camera)

    message = str(exc_info.value)
    assert "autofocus.motor.objective_ranges.4x.min_pos" in message
    assert "objective_switch_collision_limit_pos (-3168285)" in message


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
    assert "objectives.4x.switch.focus_collision_limit_pos" in message
    assert "objectives.hardware.focus_axis.objective_switch_collision_limit_pos" in message


def test_plates_validator_rejects_misplaced_runtime_guard_and_legacy_fields() -> None:
    base_plate = {
        "rows": 1,
        "cols": 1,
        "a1_start": {"x": 0, "y": 0},
        "well_diameter_mm": 1.0,
        "well_gap_mm": 0.0,
        "well_step": {"col": {"x": 0, "y": 0}, "row": {"x": 0, "y": 0}},
        "pulses_per_mm": 100,
        "x_stage_sign_for_view_right": -1,
        "y_stage_sign_for_view_down": -1,
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
            "6-well": {**base_plate, "row_stage_sign": -1},
            "12-well": {
                **base_plate,
                "point_12": [0, 0],
                "x_stage_sign_for_view_down": -1,
            },
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
    assert "plates.12-well.x_stage_sign_for_view_down" in message


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
