"""Camera background failures retain the run that owns the camera session."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
import threading
import time

import pytest

from workflow import alarm_service as alarms
from workflow import camera_process_supervisor as camera
from workflow.task_logging import current_request_id, current_task_id


@pytest.fixture
def service(tmp_path, monkeypatch):
    instance = alarms.AlarmService(tmp_path / "alarms.db")
    monkeypatch.setattr(alarms, "_service", instance)
    instance.start_writer()
    yield instance
    instance.stop_writer()


def query(service, **kwargs):
    assert service.flush_device_events()
    return service.query(**kwargs)


@contextmanager
def origin(task="task-a", execution="run-a", request="request-a"):
    task_token = current_task_id.set(task)
    request_token = current_request_id.set(request)
    try:
        with alarms.alarm_context(task, execution):
            yield
    finally:
        current_request_id.reset(request_token)
        current_task_id.reset(task_token)


def wait_for_events(service, count=1):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        events = query(service)["items"]
        if len(events) >= count:
            return events
        time.sleep(0.01)
    pytest.fail("background alarm did not arrive")


def test_monitor_failure_uses_session_run_and_request(service, monkeypatch):
    supervisor = camera.CameraProcessSupervisor()
    observed = []
    monkeypatch.setattr(supervisor, "_terminate_process", lambda *args: observed.append(
        (alarms.get_alarm_context(), current_task_id.get(), current_request_id.get())))
    with origin():
        supervisor._transition("opening", session_id="session-a")
        supervisor._transition("open", session_id="session-a")
    thread = threading.Thread(target=lambda: supervisor._discard_worker_locked(
        camera.CameraWorkerError("private detail", error_type="CAMERA_RECORDING_STALLED"), restart=False))
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    event = query(service)["items"][0]
    assert (event["task_id"], event["execution_id"]) == ("task-a", "run-a")
    assert observed == [(("task-a", "run-a"), "task-a", "request-a")]
    assert alarms.get_alarm_context() == (None, None)


def test_next_unowned_session_does_not_inherit_prior_run(service):
    supervisor = camera.CameraProcessSupervisor()
    with origin():
        supervisor._transition("opening", session_id="old")
    supervisor._clear_session()
    supervisor._transition("opening", session_id="new")
    supervisor._transition("open", session_id="new")
    supervisor._discard_worker_locked(camera.CameraProcessError("worker died"), restart=False)
    item = query(service)["items"][0]
    assert item["task_id"] is None and item["execution_id"] is None


def test_async_start_failure_preserves_context_after_session_cleanup(service, monkeypatch):
    supervisor = camera.CameraProcessSupervisor()
    observed = []
    def fail_start():
        observed.append((current_task_id.get(), current_request_id.get()))
        raise camera.CameraProcessTimeout("worker startup timeout")
    monkeypatch.setattr(supervisor, "_spawn_worker_locked", fail_start)
    with origin():
        supervisor.begin_start_recording({"settings": {}})
    item = wait_for_events(service)[0]
    assert item["code"] == "CAMERA_COMMAND_TIMEOUT"
    assert (item["task_id"], item["execution_id"]) == ("task-a", "run-a")
    assert observed == [("task-a", "request-a")]


def test_async_stop_finalization_uses_original_session_run(service, monkeypatch):
    supervisor = camera.CameraProcessSupervisor()
    with origin():
        supervisor._transition("starting", session_id="recording-session")
        supervisor._transition("recording", session_id="recording-session")
    monkeypatch.setattr(supervisor, "_spawn_worker_locked", lambda: None)
    monkeypatch.setattr(supervisor, "_request_locked", lambda *args: {"video": {}})
    with origin("stop-caller", "stop-run", "stop-request"):
        supervisor.begin_stop_recording()
    item = wait_for_events(service)[0]
    assert item["code"] == "CAMERA_RECORD_RESULT_MISSING"
    assert (item["task_id"], item["execution_id"]) == ("task-a", "run-a")


def test_explicit_task_using_shared_camera_takes_precedence(service, monkeypatch):
    supervisor = camera.CameraProcessSupervisor()
    with origin():
        supervisor._transition("starting", session_id="shared-session")
        supervisor._transition("recording", session_id="shared-session")
    monkeypatch.setattr(supervisor, "_spawn_worker_locked", lambda: None)
    with origin("task-b", "run-b", "request-b"):
        supervisor._discard_worker_locked(camera.CameraProcessTimeout("task capture failed"), restart=False)
    item = query(service)["items"][0]
    assert (item["task_id"], item["execution_id"]) == ("task-b", "run-b")


def test_normal_stop_history_is_info_once_after_finalization(service, monkeypatch):
    supervisor = camera.CameraProcessSupervisor()
    with origin():
        supervisor._transition("starting", session_id="recording-session", operation_id="recording-op")
        supervisor._transition("recording", session_id="recording-session", operation_id="recording-op")
    monkeypatch.setattr(supervisor, "_spawn_worker_locked", lambda: None)
    monkeypatch.setattr(supervisor, "_request_locked", lambda *args: {"video": {}})
    info = camera.RemoteVideoInfo.from_payload({"saved_path": "private/video.avi", "frame_count": 10})
    monkeypatch.setattr(supervisor, "_finalize_recording_video", lambda *args: info)
    context = supervisor._reserve_recording_stop()
    assert supervisor._complete_recording_stop(context, join_timeout_s=1) == info
    assert supervisor._complete_recording_stop(context, join_timeout_s=1) == info
    events = query(service)["items"]
    assert len(events) == 1
    item = events[0]
    assert item["code"] == "CAMERA_RECORDING_STOPPED"
    assert item["severity"] == "INFO" and not item["active"]
    assert item["execution_id"] == "run-a"
    assert "private" not in str(item)


@pytest.mark.parametrize("startup_failure", ["timeout", "exception", "handshake"])
def test_restart_failure_has_its_own_alarm_and_deduplicates_cleanup(service, monkeypatch, startup_failure):
    parent, child, process = Mock(), Mock(), Mock()
    parent.poll.return_value = startup_failure != "timeout"
    parent.recv.return_value = {"kind": "invalid"}
    if startup_failure == "exception":
        process.start.side_effect = OSError("private startup error")
    supervisor = camera.CameraProcessSupervisor(mp_context=SimpleNamespace(
        Pipe=Mock(return_value=(parent, child)), Process=Mock(return_value=process)))
    monkeypatch.setattr(supervisor, "_terminate_process", lambda *args: None)
    with origin():
        supervisor._transition("opening", session_id="session-a")
        supervisor._transition("open", session_id="session-a")
    error = camera.CameraProcessError("initial worker death")
    supervisor._discard_worker_locked(error, restart=True)
    camera._report_camera_failure(error)
    events = query(service)["items"]
    restart = [e for e in events if e["code"] == "CAMERA_WORKER_RESTART_FAILED"]
    assert len(restart) == 1 and restart[0]["count"] == 1
    assert restart[0]["execution_id"] == "run-a"
    assert supervisor.status()["error_code"] == "CAMERA_WORKER_RESTART_FAILED"
    assert len(events) == 3


def test_camera_failure_deduplication_is_per_code(service):
    error = camera.CameraProcessTimeout("private")
    for _ in range(2):
        camera._report_camera_failure(error)
        camera._report_camera_failure(error, "CAMERA_WORKER_RESTART_FAILED")
    assert [e["code"] for e in query(service)["items"]] == [
        "CAMERA_COMMAND_TIMEOUT", "CAMERA_WORKER_RESTART_FAILED"]


@pytest.mark.parametrize("operation", ["capture", "record_video"])
@pytest.mark.parametrize("failure", ["discard", "same_session", "new_session"])
def test_old_response_cannot_recover_new_camera_failure(service, monkeypatch, operation, failure):
    supervisor = camera.CameraProcessSupervisor()
    supervisor._transition("open", session_id="old")
    response = {"frame": {"width": 32, "height": 24, "frame_len": 768}, "video": {"frame_count": 10}}

    def failure_before_response_is_consumed(*args):
        if failure == "discard":
            supervisor._discard_worker_locked(camera.CameraProcessError("worker died"), restart=False)
        else:
            if failure == "new_session":
                supervisor._transition("open", session_id="new")
            supervisor._report_failure(camera.CameraWorkerError("new SDK error", error_type="SDKError"))
        return response

    monkeypatch.setattr(supervisor, "_request", failure_before_response_is_consumed)
    if operation == "capture":
        supervisor.capture("old", "unused")
    else:
        supervisor.record_video("old", "unused", duration_s=1, fps=10, bitrate_kbps=100, timeout_ms=1000)
    active = query(service, mode="snapshot")["items"]
    assert len(active) == 1 and active[0]["active"]
    assert [e["transition"] for e in query(service)["items"]] == ["occurred"]
    # New evidence after the failure can recover, including in the same session.
    supervisor._transition("open", session_id="fresh")
    monkeypatch.setattr(supervisor, "_request", lambda *args: response)
    supervisor.capture("fresh", "unused")
    assert not query(service, mode="snapshot")["items"]


@pytest.mark.parametrize("video_problem", [None, "empty_frames", "empty_file", "missing_file", "new_failure"])
def test_recording_finalization_recovers_only_verified_current_recording(
        service, monkeypatch, tmp_path, video_problem):
    supervisor = camera.CameraProcessSupervisor()
    supervisor._report_failure(camera.CameraWorkerError("old stall", error_type="CAMERA_RECORDING_STALLED"))
    video_path = tmp_path / "recording.avi"
    if video_problem != "missing_file":
        video_path.write_bytes(b"" if video_problem == "empty_file" else b"test video bytes")
    supervisor._transition("starting", session_id="new", operation_id="new-op")
    supervisor._transition("recording", session_id="new", operation_id="new-op")
    monkeypatch.setattr(supervisor, "_spawn_worker_locked", lambda: None)
    monkeypatch.setattr(supervisor, "_request_locked", lambda *args: {
        "video": {"saved_path": str(video_path), "frame_count": 0 if video_problem == "empty_frames" else 10}})
    if video_problem == "new_failure":
        original = supervisor._finalize_recording_video
        def finalize(*args):
            info = original(*args)
            supervisor._report_failure(camera.CameraProcessError("new failure during finalization"))
            return info
        monkeypatch.setattr(supervisor, "_finalize_recording_video", finalize)
    if video_problem in {"empty_frames", "empty_file", "missing_file"}:
        with pytest.raises(camera.CameraWorkerError):
            supervisor.stop_recording()
    else:
        assert supervisor.stop_recording().frame_count == 10
    active = {e["code"] for e in query(service, mode="snapshot")["items"]}
    if video_problem is None:
        assert not active
        assert [e["transition"] for e in query(service)["items"]
                if e["code"] == "CAMERA_RECORDING_STALLED"] == ["occurred", "recovered"]
    else:
        assert "CAMERA_RECORDING_STALLED" in active
        if video_problem == "new_failure":
            assert "CAMERA_WORKER_FAILED" in active


def test_monitor_response_cannot_clear_failure_published_during_status_request(service, monkeypatch):
    supervisor = camera.CameraProcessSupervisor()
    supervisor._transition("recording", session_id="current")
    supervisor._monitor_stop = Mock()
    supervisor._monitor_stop.wait.side_effect = [False, True]

    def status_response(*args):
        supervisor._report_failure(camera.CameraWorkerError("new fault", error_type="SDKError"))
        return {"recording": True, "background": True, "frame_count": 10}

    monkeypatch.setattr(supervisor, "_request_locked", status_response)
    supervisor._monitor_loop()
    assert [e["code"] for e in query(service, mode="snapshot")["items"]] == ["CAMERA_SDK_ERROR"]
    assert [e["transition"] for e in query(service)["items"]] == ["occurred"]
