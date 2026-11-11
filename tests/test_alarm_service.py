from types import SimpleNamespace

import pytest

import workflow.alarm_service as alarms
from workflow.alarm_store import AlarmQueryError


@pytest.fixture
def service(tmp_path, monkeypatch):
    service = alarms.AlarmService(tmp_path / "alarms.db")
    monkeypatch.setattr(alarms, "_service", service)
    return service


def test_unregistered_error_has_only_safe_public_message(service):
    alarms.report_alarm("C:/private/secret token=123", component="test")
    item = service.query()["items"][0]
    assert item["code"] == "UNKNOWN_ERROR"
    assert "secret" not in str(item)
    assert item["diagnostic_id"]


def test_context_links_root_cause_and_task_impact(service):
    with alarms.alarm_context("task1", "exec1"):
        alarms.report_alarm("UNKNOWN_ERROR", component="camera")
        alarms.report_alarm("TASK_FAILED", component="task", fact=True, occurrence_key="exec1")
    items = service.query()["items"]
    assert len(items) == 2
    assert {e["execution_id"] for e in items} == {"exec1"}
    assert {e["task_id"] for e in items} == {"task1"}


def test_write_failure_does_not_replace_business_exception(service, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(service.store, "record", lambda **kw: (_ for _ in ()).throw(OSError("disk full")))
        assert alarms.report_alarm("UNKNOWN_ERROR", component="test") is None
        assert service.query()["monitoring"]["store"]["recording_gap_possible"]
    mock_probes(monkeypatch)
    service.sample()
    assert not service.query()["monitoring"]["store"]["recording_gap_possible"]
    assert "ALARM_RECORDING_INTERRUPTED" in {e["code"] for e in service.query()["items"]}


def mock_probes(monkeypatch, *, worker=True, free=80):
    monkeypatch.setattr("workflow.alarm_task_hooks.reconcile_task_alarms", lambda: {})
    monkeypatch.setattr("workflow.task_runtime.get_task_runtime_health",
                        lambda: {"worker_alive": worker, "status": "healthy" if worker else "failed"})
    monkeypatch.setattr(alarms.shutil, "disk_usage", lambda p: SimpleNamespace(total=100, free=free))


def test_monitor_reports_worker_and_disk_without_control_actions(service, monkeypatch):
    mock_probes(monkeypatch, worker=False, free=5)
    service.sample()
    codes = {e["code"] for e in service.query()["items"]}
    assert {"TASK_WORKER_UNAVAILABLE", "DISK_SPACE_LOW", "DISK_SPACE_CRITICAL"} <= codes
    count = len(service.query()["items"])
    service.sample()
    assert len(service.query()["items"]) == count
    mock_probes(monkeypatch, worker=True, free=80)
    service.sample()
    assert not service.query(mode="snapshot")["items"]
    assert service.query()["monitoring"]["hardware"]["status"] == "unknown"


def test_read_failure_is_503_not_empty_healthy(service, monkeypatch):
    monkeypatch.setattr(service.store, "query", lambda **kw: (_ for _ in ()).throw(OSError("secret path")))
    with pytest.raises(AlarmQueryError) as error:
        service.query()
    assert error.value.status == 503
    assert "secret" not in str(error.value.detail)


def test_query_does_not_probe_devices_or_sample(service, monkeypatch):
    monkeypatch.setattr(service, "sample", lambda: pytest.fail("GET must never probe"))
    assert service.query()["items"] == []
    assert service.query()["monitoring"]["monitor"]["status"] == "starting"


def test_stale_observation_is_not_healthy(service, monkeypatch):
    mock_probes(monkeypatch)
    service.sample()
    service._sample_time -= 60
    assert service.monitoring()["monitor"]["status"] == "stale"


def test_no_service_is_noop_for_hooks(monkeypatch):
    monkeypatch.setattr(alarms, "_service", None)
    assert alarms.report_alarm("UNKNOWN_ERROR", component="test") is None
    assert alarms.recover_alarm("UNKNOWN_ERROR", component="test") is None


def test_inactive_recovery_uses_no_sqlite_io_and_restart_loads_active_state(service, monkeypatch):
    alarms.report_alarm("CAMERA_SDK_ERROR", component="camera")
    restarted = alarms.AlarmService(service.path)
    assert restarted.record("CAMERA_SDK_ERROR", component="camera", recovery=True)
    monkeypatch.setattr(restarted.store, "record", lambda **kw: pytest.fail("inactive recovery opened database"))
    for _ in range(100):
        assert restarted.record("CAMERA_SDK_ERROR", component="camera", recovery=True) is None


def test_failed_monitor_recovery_is_retried(service, monkeypatch):
    mock_probes(monkeypatch, worker=False)
    service.sample()
    mock_probes(monkeypatch, worker=True)
    original = service.store.record
    with monkeypatch.context() as patch:
        def fail_recovery(**kwargs):
            if kwargs.get("recovery"):
                raise OSError("disk full")
            return original(**kwargs)
        patch.setattr(service.store, "record", fail_recovery)
        service.sample()
    service.sample()
    items = service.query()["items"]
    assert [e["transition"] for e in items if e["code"] == "TASK_WORKER_UNAVAILABLE"] == ["occurred", "recovered"]


def test_monitor_thread_start_failure_is_fail_open(service, monkeypatch):
    monkeypatch.setattr(alarms.threading.Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError("thread allocation")))
    service.start()
    assert service.monitoring()["monitor"]["status"] == "failed"
    service.stop()


def test_monitor_recovers_fault_reported_between_samples(service, monkeypatch):
    mock_probes(monkeypatch, worker=True)
    service.sample()
    alarms.report_alarm("TASK_WORKER_UNAVAILABLE", component="task_runtime")
    service.sample()
    items = service.query()["items"]
    assert [e["transition"] for e in items if e["code"] == "TASK_WORKER_UNAVAILABLE"] == ["occurred", "recovered"]


@pytest.mark.parametrize("warning,critical", [
    ("10", "20"), ("10", "10"), ("100", "10"), ("20", "0"),
    ("invalid", "10"), ("20", ""), ("nan", "10"), ("20", "nan"),
    ("inf", "10"), ("20", "-inf"),
])
def test_invalid_disk_thresholds_keep_recording_and_monitoring(tmp_path, monkeypatch, warning, critical):
    monkeypatch.setattr(alarms, "_service", None)
    monkeypatch.setenv("COLONY_ALARM_DB", str(tmp_path / "alarms.db"))
    monkeypatch.setenv("COLONY_ALARM_DISK_WARNING_PERCENT", warning)
    monkeypatch.setenv("COLONY_ALARM_DISK_CRITICAL_PERCENT", critical)
    service = alarms.initialize_alarm_service()
    assert alarms.get_alarm_service() is service
    alarms.report_alarm("CAMERA_SDK_ERROR", component="camera")
    mock_probes(monkeypatch, free=15)
    service.sample()
    response = service.query(mode="snapshot")
    assert {e["code"] for e in response["items"]} == {
        "ALARM_DISK_CONFIG_INVALID", "CAMERA_SDK_ERROR", "DISK_SPACE_LOW"}
    disk = response["monitoring"]["disk"]
    assert disk["thresholds_defaulted"] is True
    assert (disk["warning_percent"], disk["critical_percent"]) == (20, 10)
    assert disk["status"] == "warning"
    alarms.shutdown_alarm_service()


def test_disk_configuration_alarm_recovers_after_corrected_restart(tmp_path, monkeypatch):
    path = tmp_path / "alarms.db"
    service = alarms.AlarmService(path, warning_percent=10, critical_percent=20)
    assert service.query(mode="snapshot")["items"][0]["code"] == "ALARM_DISK_CONFIG_INVALID"
    restarted = alarms.AlarmService(path, warning_percent=30, critical_percent=15)
    monkeypatch.setattr(alarms, "_service", restarted)
    assert not restarted.query(mode="snapshot")["items"]
    assert [e["transition"] for e in restarted.query()["items"]] == ["occurred", "recovered"]
    mock_probes(monkeypatch, free=12)
    restarted.sample()
    assert restarted.monitoring()["disk"]["status"] == "critical"
    assert restarted.monitoring()["disk"]["thresholds_defaulted"] is False
