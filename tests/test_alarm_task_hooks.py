"""Real SQLite alarm integration with deterministic task/vision/storage failures."""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from workflow import alarm_service, alarm_task_hooks, task_store, task_runtime
from workflow.api_models import ExecuteTaskRequest


@pytest.fixture
def service(tmp_path, monkeypatch):
    instance = alarm_service.AlarmService(tmp_path / "alarm.sqlite3", device_id="unit-device")
    assert instance.store is not None
    monkeypatch.setattr(alarm_service, "_service", instance)
    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path / "task_index"))
    alarm_task_hooks.reset_task_alarm_reconciliation()
    instance.start_writer()
    yield instance
    instance.stop()
    instance.stop_writer()
    alarm_task_hooks.reset_task_alarm_reconciliation()


def events(service, code=None):
    assert service.flush_device_events()
    values = service.store.query()["items"]
    return [item for item in values if code is None or item["code"] == code]


def test_each_acceptance_has_unique_execution_and_failure_history_survives_success(service):
    task = {"task_id": "reusable", "task_type": "capture"}
    first = task_store.create_accepted_task_record_if_allowed(task, None, False)
    failed = task_store.finalize_failed_record(first, task, "internal message", None, False)
    task_store.write_task_record(failed)
    alarm_task_hooks.reconcile_task_alarms()
    second = task_store.create_accepted_task_record_if_allowed(task, None, False)
    assert first["execution_id"] != second["execution_id"]
    success = task_store.finalize_success_record(second, task, {"status": "success"}, None, False)
    task_store.write_task_record(success)
    failures = events(service, "TASK_FAILED")
    assert len(failures) == 1
    assert failures[0]["execution_id"] == first["execution_id"]
    assert "internal message" not in json.dumps(failures)


def test_reconcile_legacy_and_interrupted_idempotently_and_retry_failed_recording(service, monkeypatch):
    directory = task_store.task_index_dir()
    directory.mkdir()
    legacy = {"task_id": "old", "status": "failed", "created_at": "2026-01-01T00:00:00Z",
              "finished_at": "2026-01-01T00:00:01Z"}
    (directory / "old.json").write_text(json.dumps(legacy), encoding="utf-8")
    original = service.record
    monkeypatch.setattr(service, "record", lambda *args, **kwargs: None)
    assert alarm_task_hooks.reconcile_task_alarms()["reconciled"] == 0
    monkeypatch.setattr(service, "record", original)
    assert alarm_task_hooks.reconcile_task_alarms()["reconciled"] == 1
    alarm_task_hooks.reset_task_alarm_reconciliation()
    alarm_task_hooks.reconcile_task_alarms()
    item = events(service, "TASK_FAILED")[0]
    assert len(events(service, "TASK_FAILED")) == 1
    assert item["execution_id"].startswith("legacy-")
    assert item["occurred_at"].startswith("2026-01-01T00:00:01")
    task_store.create_accepted_task_record_if_allowed({"task_id": "interrupted"}, None, False)
    assert task_store.recover_interrupted_task_records()["interrupted"] == 1
    alarm_task_hooks.reconcile_task_alarms()
    assert len(events(service, "TASK_INTERRUPTED")) == 1


def test_failed_task_keeps_run_correlation_even_if_terminal_json_write_fails(service, monkeypatch):
    task = {"task_id": "broken", "task_type": "capture"}
    accepted = task_store.create_accepted_task_record_if_allowed(task, None, False)
    req = ExecuteTaskRequest(task=task)

    def execute(**kwargs):
        alarm_service.report_alarm("VISION_INFERENCE_FAILED", component="vision")
        raise RuntimeError("sensitive SDK detail")

    def fail_write(record):
        raise OSError("disk full")

    monkeypatch.setattr(task_runtime, "write_task_record", fail_write)
    with pytest.raises(OSError, match="disk full"):
        task_runtime.run_task_async(task, req, threading.Event(), task_executor=execute,
                                    execution_id=accepted["execution_id"])
    recorded = events(service)
    assert {item["code"] for item in recorded} >= {"TASK_FAILED", "VISION_INFERENCE_FAILED"}
    assert all(item["execution_id"] == accepted["execution_id"] for item in recorded)
    assert all(item["task_id"] == "broken" for item in recorded)


def test_cancellation_is_info_history_and_admission_busy_is_not_alarm(service):
    record = task_store.create_accepted_task_record_if_allowed({"task_id": "cancel"}, None, False)
    task_store.write_task_record(task_store.mark_record_canceled(record, "operator"))
    assert events(service, "TASK_CANCELED")[0]["severity"] == "INFO"
    alarm_task_hooks.observe_task_record({**record, "status": "failed", "error_code": "HARDWARE_BUSY"})
    assert not events(service, "TASK_FAILED")
    assert not service.store.query(mode="snapshot")["items"]


def test_storage_write_failure_and_same_file_recovery_preserve_original_exception(service, monkeypatch, tmp_path):
    from workflow import file_io
    target = tmp_path / "private-location" / "result.json"
    original = file_io.os.replace
    with monkeypatch.context() as patch:
        patch.setattr(file_io.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk full secret")))
        with pytest.raises(OSError, match="disk full secret"):
            file_io.atomic_write_json(target, {}, attempts=1, sleep_s=0)
    file_io.atomic_write_json(target, {})
    values = events(service, "STORAGE_WRITE_FAILED")
    assert [item["transition"] for item in values] == ["occurred", "recovered"]
    assert str(target) not in json.dumps(values)
    assert original is file_io.os.replace


def test_alarm_write_failure_does_not_mask_file_write_failure(service, monkeypatch, tmp_path):
    from workflow import file_io
    monkeypatch.setattr(service.store, "record", lambda **kwargs: (_ for _ in ()).throw(OSError("alarm unavailable")))
    monkeypatch.setattr(file_io.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("original failure")))
    with pytest.raises(OSError, match="original failure"):
        file_io.atomic_write_text(tmp_path / "data", "x", attempts=1, sleep_s=0)


def test_invalid_task_record_and_result_read_recover_after_success(service, tmp_path):
    from workflow.task_artifacts import build_task_result_response, TaskArtifactError
    path = task_store.task_record_path("invalid")
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(task_store.TaskStoreError):
        task_store.read_task_record("invalid")
    path.write_text('{"task_id":"invalid","status":"success"}', encoding="utf-8")
    task_store.read_task_record("invalid")
    assert [item["transition"] for item in events(service, "TASK_RECORD_READ_FAILED")] == ["occurred", "recovered"]
    output = tmp_path / "result.json"
    output.write_text("[]", encoding="utf-8")
    record = {"task_id": "invalid", "status": "success", "result_json_path": str(output)}
    # The production path guard must still be respected by the query helper.
    from unittest.mock import patch
    with patch("workflow.task_artifacts.resolve_output_path", side_effect=lambda value, field: value):
        with pytest.raises(TaskArtifactError):
            build_task_result_response(record, {"running"})
        output.write_text("{}", encoding="utf-8")
        build_task_result_response(record, {"running"})
    assert [item["transition"] for item in events(service, "RESULT_READ_FAILED")] == ["occurred", "recovered"]


@pytest.mark.parametrize("exception_name,code", [
    ("VisionInputQualityError", "VISION_INPUT_QUALITY_FAILED"),
    ("VisionModelError", "VISION_MODEL_FAILED"),
    ("RuntimeError", "VISION_INFERENCE_FAILED"),
])
def test_detector_errors_recover_on_real_success_and_empty_result_is_valid(service, monkeypatch, exception_name, code):
    from workflow import detect_api
    exception_type = type(exception_name, (RuntimeError,), {})
    def fail(*args, **kwargs):
        raise exception_type("private model path /secret")
    monkeypatch.setattr(detect_api, "_resolve_callable", lambda _: fail)
    with pytest.raises(exception_type):
        detect_api.run_detect_on_image("example.bmp")
    monkeypatch.setattr(detect_api, "_resolve_callable", lambda _: lambda *args: {"components": []})
    assert detect_api.run_detect_on_image("example.bmp")["clone_count"] == 0
    assert [item["transition"] for item in events(service, code)] == ["occurred", "recovered"]
    assert "private model path" not in json.dumps(events(service))


def test_gpu_fallback_is_reported_and_explicit_cpu_does_not_prove_recovery(service, monkeypatch):
    from workflow import detect_api
    raw = {"components": [], "runtime": {"backend": "CPUExecutionProvider", "fallback_reasons": {"detector": "private error"}}}
    monkeypatch.setattr(detect_api, "_resolve_callable", lambda _: lambda *args: raw)
    detect_api.run_detect_on_image("example.bmp")
    raw["runtime"]["fallback_reasons"] = {}
    detect_api.run_detect_on_image("example.bmp")
    assert len(events(service, "VISION_GPU_DEGRADED")) == 1
    raw["runtime"]["backend"] = "detector=CUDAExecutionProvider;segmenter=CUDAExecutionProvider"
    detect_api.run_detect_on_image("example.bmp")
    assert [item["transition"] for item in events(service, "VISION_GPU_DEGRADED")] == ["occurred", "recovered"]


@pytest.mark.parametrize("metadata", [
    {"runtime": "custom metadata"}, {"texture_processing": "custom metadata"},
    {"texture_processing": {"stage_backends": ["cpu"]}},
])
def test_custom_detector_metadata_does_not_break_successful_inference(service, monkeypatch, metadata):
    from workflow import detect_api
    monkeypatch.setattr(detect_api, "_resolve_callable", lambda _: lambda *args: {"components": [], **metadata})
    assert detect_api.run_detect_on_image("example.bmp")["clone_count"] == 0


def test_configuration_preflight_error_does_not_change_exception_or_clear_when_skipped(service, monkeypatch):
    from workflow import config_loader, run_task
    with pytest.raises(FileNotFoundError):
        config_loader.load_runtime_context("missing-task-config.yaml")
    assert events(service, "CONFIG_EXECUTION_BLOCKED")
    alarm_service.report_alarm("CONFIG_EXECUTION_BLOCKED", component="configuration", resource="detection_preflight")
    before = len(events(service))
    run_task.preflight_detection_backend({}, {"stages": []})
    assert len(events(service)) == before


def test_skipped_and_canceled_scan_preflight_do_not_report_or_clear_fault(service):
    from workflow.run_task import preflight_five_point_scans
    from workflow.task_control import TaskCanceled
    alarm_service.report_alarm("CONFIG_EXECUTION_BLOCKED", component="configuration", resource="scan_preflight")
    before = len(events(service))
    preflight_five_point_scans({}, {"scan_mode": "stop_shoot", "stages": ["capture"]})
    with pytest.raises(TaskCanceled):
        preflight_five_point_scans({}, {"scan_mode": "five_point", "stages": ["capture"],
                                      "observe_scope": "single_well", "well_name": "A1", "_cancel_check": True})
    assert len(events(service)) == before


def test_compensation_unverified_final_offset_reports_and_verified_target_recovers(service, monkeypatch):
    from workflow import compensate_executor as comp
    monkeypatch.setattr(comp, "_capture_closed_loop_image", lambda **kwargs: {"saved_path": "frame.bmp"})
    monkeypatch.setattr(comp, "_build_image_item_from_capture", lambda **kwargs: {})
    monkeypatch.setattr(comp, "select_clone_for_compensation", lambda *args: ({}, {}))
    monkeypatch.setattr(comp, "_calc_compensate_target", lambda **kwargs: {
        "offset_from_image_center_px": [20, 20], "offset_mm": [1, 1], "compensate_target": {"x": 1, "y": 1}})
    monkeypatch.setattr(comp, "_move_to_compensate_target", lambda **kwargs: {})
    kwargs = {"ctx": {}, "params": {"compensate_closed_loop": {"max_iterations": 1}},
              "first_move_result": {}, "initial_target": {"x": 0, "y": 0}}
    monkeypatch.setattr(comp, "_within_tolerance", lambda *args: False)
    assert comp._run_closed_loop(**kwargs)["final_in_tolerance"] is False
    monkeypatch.setattr(comp, "_within_tolerance", lambda *args: True)
    assert comp._run_closed_loop(**kwargs)["final_in_tolerance"] is True
    assert [item["transition"] for item in events(service, "COMPENSATION_OUT_OF_TOLERANCE")] == ["occurred", "recovered"]


def test_worker_health_distinguishes_dead_thread_from_intentional_stop(service, monkeypatch):
    manager = task_runtime.TaskRuntimeManager()
    monkeypatch.setattr(task_runtime, "DEFAULT_TASK_RUNTIME_MANAGER", manager)
    manager._accepting = True
    assert task_runtime.get_task_runtime_health()["status"] == "failed"
    manager._accepting = False
    assert task_runtime.get_task_runtime_health()["status"] == "stopped"


def test_unhandled_api_fault_recorded_but_validation_not_alarm(service):
    from starlette.requests import Request
    from fastapi.exceptions import RequestValidationError
    from workflow.api_errors import unhandled_exception_handler, request_validation_exception_handler
    request = Request({"type": "http", "method": "GET", "path": "/example", "headers": [],
                       "query_string": b"", "state": {"request_id": "req-unit"}})
    response = asyncio.run(unhandled_exception_handler(request, RuntimeError("/private/trace")))
    assert response.status_code == 500
    assert len(events(service, "API_UNHANDLED_ERROR")) == 1
    assert events(service, "API_UNHANDLED_ERROR")[0]["diagnostic_id"] == "req-unit"
    asyncio.run(request_validation_exception_handler(request, RequestValidationError([])))
    assert len(events(service)) == 1


def test_recording_file_configuration_alarm_excludes_invalid_request_overrides(service, monkeypatch):
    from workflow import camera_record_service as camera
    from workflow.api_models import CameraRecordStartRequest
    from workflow.config_validator import ConfigValidationError
    req = CameraRecordStartRequest(save_path="outputs/test.avi")
    with monkeypatch.context() as patch:
        patch.setattr(camera, "validate_camera_file", lambda path: (_ for _ in ()).throw(FileNotFoundError("private config")))
        with pytest.raises(FileNotFoundError):
            camera.load_camera_settings_for_recording(req)
    monkeypatch.setattr(camera, "validate_camera_file", lambda path: {"camera": {"mvs_python_dir": "sdk"}})
    monkeypatch.setattr(camera, "resolve_mvs_python_dir", lambda cfg: "sdk")
    camera.load_camera_settings_for_recording(req)
    assert [e["transition"] for e in events(service, "CONFIG_EXECUTION_BLOCKED")] == ["occurred", "recovered"]
    before = len(events(service))
    req.ip = "bad-user-override"
    monkeypatch.setattr(camera, "validate_camera_config", lambda *a, **kw: (_ for _ in ()).throw(ValueError("invalid override")))
    with pytest.raises(ValueError):
        camera.load_camera_settings_for_recording(req)
    assert len(events(service)) == before
