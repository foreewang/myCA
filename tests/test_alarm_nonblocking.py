"""Blocked alarm persistence must not sit between a motion fault and stopping."""
from contextlib import nullcontext
from types import SimpleNamespace
import threading

import pytest

from devices.motion.modbus import ModbusRTUClient
from workflow import alarm_service as alarms, stage_executor as stage
from workflow.task_control import TaskCanceled


@pytest.fixture
def service(tmp_path, monkeypatch):
    instance = alarms.AlarmService(tmp_path / "alarm.db")
    monkeypatch.setattr(alarms, "_service", instance)
    instance.start_writer()
    yield instance
    instance.stop_writer()


@pytest.mark.parametrize("failure", ["limit", "timeout", "generic", "cancel"])
@pytest.mark.parametrize("first_stop_fails", [False, True])
def test_both_stop_commands_run_while_alarm_database_is_blocked(
        service, monkeypatch, failure, first_stop_fails):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = service.store.record

    def blocked_record(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(service.store, "record", blocked_record)
    alarms.report_device_alarm("UNKNOWN_ERROR", component="test")
    assert entered.wait(2)
    stops, errors = [], []
    client = ModbusRTUClient(port="fake")

    def write_stop(slave, value):
        stops.append(slave)
        return not (first_stop_fails and slave == 1)

    monkeypatch.setattr(client, "_write_controlword", write_stop)
    monkeypatch.setattr(stage, "ModbusRTUClient", lambda **kwargs: nullcontext(client))
    monkeypatch.setattr(stage, "MotorManager", lambda c, slave: SimpleNamespace(client=c, slave=slave))
    monkeypatch.setattr(stage, "snapshot_xy", lambda *a: {
        axis: {"current_pos": 0, "statusword": 0x427} for axis in ("x", "y")})
    for name in ("_ensure_xy_ready", "_write_axis_pp_target", "_trigger_xy_pp"):
        monkeypatch.setattr(stage, name, lambda *a, **kw: None)

    def fail(**kwargs):
        if failure == "limit":
            stage._check_current_within_hard_limits(
                current={"x": 101, "y": 0},
                stage_limits={"enabled": True, "x_min": -100, "x_max": 100,
                              "y_min": -100, "y_max": 100})
        if failure == "timeout":
            raise stage._stage_failure("timeout", "MOTION_TIMEOUT")
        if failure == "cancel":
            raise TaskCanceled("cancel")
        raise RuntimeError("motion failed")

    monkeypatch.setattr(stage, "_wait_xy_arrival", fail)

    def run():
        try:
            stage.move_to_absolute(port="fake", x_target=1, y_target=1,
                                   profile_vel=1, profile_acc=1, profile_dec=1)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert finished.wait(2), "motion waited for alarm persistence before stopping"
        assert not release.is_set()
        assert stops == [1, 2]
        assert len(errors) == 1
        assert isinstance(errors[0], TaskCanceled if failure == "cancel" else
                          RuntimeError if failure == "generic" else stage.StageMotionError)
    finally:
        release.set()
        worker.join(2)
        assert service.flush_device_events()


def test_async_events_keep_order_occurrence_time_and_origin(service, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = service.store.record

    def blocked_record(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(service.store, "record", blocked_record)
    try:
        with alarms.alarm_context("task-a", "run-a"):
            alarms.report_device_alarm("MOTION_TIMEOUT", component="stage")
        assert entered.wait(2)
        with alarms.alarm_context("task-b", "run-b"):
            alarms.recover_device_alarm("MOTION_TIMEOUT", component="stage")
        # GET remains read-only and does not flush/wait for the writer.
        assert service.query()["items"] == []
    finally:
        release.set()
    assert service.flush_device_events()
    items = service.query()["items"]
    assert [e["transition"] for e in items] == ["occurred", "recovered"]
    assert {e["execution_id"] for e in items} == {"run-a"}
    assert all(e["occurred_at"] <= e["recorded_at"] for e in items)


def test_full_queue_drops_without_sync_fallback_and_marks_gap(service, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = service.store.record

    def blocked_record(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(service.store, "record", blocked_record)
    alarms.report_device_alarm("UNKNOWN_ERROR", component="test")
    assert entered.wait(2)
    try:
        # The writer is blocked outside the queue, so capacity can be reduced.
        service._pending.maxsize = 1
        alarms.report_device_alarm("MOTION_TIMEOUT", component="stage")
        alarms.recover_device_alarm("MOTION_TIMEOUT", component="stage")
        health = service.monitoring()["store"]
        assert health["dropped_device_events"] == 1
        assert health["recording_gap_possible"]
    finally:
        release.set()
        service.stop_writer()
    assert not service._writer.is_alive()
    assert len(service.query()["items"]) == 2
    monkeypatch.setattr("workflow.alarm_task_hooks.reconcile_task_alarms", lambda: {})
    monkeypatch.setattr("workflow.task_runtime.get_task_runtime_health", lambda: {"worker_alive": True})
    monkeypatch.setattr(alarms.shutil, "disk_usage", lambda _: SimpleNamespace(total=100, free=80))
    service.sample()
    assert "ALARM_RECORDING_INTERRUPTED" in {e["code"] for e in service.query()["items"]}


def test_writer_start_failure_never_falls_back_to_database(tmp_path, monkeypatch):
    service = alarms.AlarmService(tmp_path / "alarm.db")
    monkeypatch.setattr(alarms, "_service", service)
    monkeypatch.setattr(threading.Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError("failed")))
    service.start_writer()
    monkeypatch.setattr(service.store, "record", lambda **kw: pytest.fail("synchronous write"))
    alarms.report_device_alarm("MOTION_TIMEOUT", component="stage")
    assert service.monitoring()["store"]["recording_gap_possible"]
    # A successful storage probe cannot make a dead writer healthy again.
    service._write_failure = False
    service._reported_drops = service._dropped_events
    assert service.monitoring()["store"]["device_writer_status"] == "failed"
    assert service.monitoring()["store"]["status"] == "degraded"
    service.stop_writer()


def test_hook_exception_is_fail_open_without_logging(service, monkeypatch):
    monkeypatch.setattr(service, "enqueue_device_event",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("queue failed")))
    monkeypatch.setattr(alarms.logger, "exception", lambda *a, **kw: pytest.fail("logging on control thread"))
    alarms.report_device_alarm("MOTION_TIMEOUT", component="stage")
    assert service.monitoring()["store"]["dropped_device_events"] == 1
