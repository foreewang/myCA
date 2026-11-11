"""Device fault boundaries, recovery evidence and unchanged error behavior."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from devices.motion import modbus
from workflow import camera_process_supervisor as camera
from workflow import objective_executor as objective
from workflow import stage_executor as stage
from workflow import stage_reciprocation as reciprocation


class EmptyReply:
    def isError(self):
        return True

    def __str__(self):
        return "No response received"


class GoodReply:
    def isError(self):
        return False


@pytest.fixture
def bus(monkeypatch):
    monkeypatch.setattr(modbus.time, "sleep", lambda _: None)
    return modbus.ModbusRTUClient(port="private-deployment-port")


def test_modbus_only_reports_exhausted_retries_and_real_reply_recovers(monkeypatch, bus):
    report, recover = Mock(), Mock()
    monkeypatch.setattr(modbus, "report_alarm", report)
    monkeypatch.setattr(modbus, "recover_alarm", recover)
    assert bus._transact("read", Mock(side_effect=[EmptyReply(), EmptyReply(), GoodReply()]), slave=1)
    report.assert_not_called()
    failed = Mock(return_value=EmptyReply())
    assert bus._transact("read", failed, slave=1) is None
    assert failed.call_count == 3
    report.assert_called_once_with("MODBUS_COMMUNICATION_FAILED", component="motion_bus", resource=bus._alarm_resource(1))
    assert "private-deployment-port" not in bus._alarm_resource(1)
    recover.reset_mock()
    assert bus._transact("read", GoodReply, slave=1)
    assert recover.call_args_list[0].args == ("MODBUS_COMMUNICATION_FAILED",)


def test_stop_ack_does_not_prove_standstill(monkeypatch, bus):
    report, recover = Mock(), Mock()
    monkeypatch.setattr(modbus, "report_alarm", report)
    monkeypatch.setattr(modbus, "recover_alarm", recover)
    monkeypatch.setattr(bus, "_write_controlword", Mock(side_effect=[False, True]))
    assert bus.quick_stop(1) is False
    assert bus.quick_stop(1) is True
    report.assert_called_once_with("MOTION_STOP_FAILED", component="motion_bus", resource=bus._alarm_resource(1))
    recover.assert_not_called()


def test_stop_exception_is_preserved_and_reported(monkeypatch, bus):
    report = Mock()
    monkeypatch.setattr(modbus, "report_alarm", report)
    error = OSError("internal serial detail")
    monkeypatch.setattr(bus, "_write_controlword", Mock(side_effect=error))
    with pytest.raises(OSError) as result:
        bus.quick_stop(1)
    assert result.value is error
    assert report.call_args.args == ("MOTION_STOP_FAILED",)


def test_camera_restart_is_not_recovery_but_successful_capture_is(monkeypatch):
    report, recover = Mock(), Mock()
    monkeypatch.setattr(camera, "report_alarm", report)
    monkeypatch.setattr(camera, "recover_alarm", recover)
    supervisor = camera.CameraProcessSupervisor()
    monkeypatch.setattr(supervisor, "_spawn_worker_locked", lambda: None)
    error = camera.CameraProcessTimeout("private SDK detail")
    supervisor._discard_worker_locked(error, restart=True)
    # Subsequent cleanup sees the same exception and must not count it twice.
    camera._report_camera_failure(error)
    report.assert_called_once_with("CAMERA_COMMAND_TIMEOUT", component="camera", fact=False)
    recover.assert_not_called()
    supervisor._transition("open", session_id="new-session")
    recover.assert_not_called()
    monkeypatch.setattr(supervisor, "_request", lambda *args: {
        "frame": {"width": 32, "height": 24, "frame_len": 768}, "status": {"opened": True}})
    supervisor.capture("new-session", "unused")
    assert "CAMERA_COMMAND_TIMEOUT" in [call.args[0] for call in recover.call_args_list]
    assert "CAMERA_RECORDING_STALLED" not in [call.args[0] for call in recover.call_args_list]


def test_camera_busy_does_not_raise_production_alarm(monkeypatch):
    report = Mock()
    monkeypatch.setattr(camera, "report_alarm", report)
    camera._report_camera_failure(camera.CameraStateError("already recording"))
    report.assert_not_called()


def test_video_finalization_failure_is_historical_fact(monkeypatch):
    report = Mock()
    monkeypatch.setattr(camera, "report_alarm", report)
    supervisor = camera.CameraProcessSupervisor()
    supervisor._transition("recording", session_id="recording-session")
    monkeypatch.setattr(supervisor, "_spawn_worker_locked", lambda: None)
    monkeypatch.setattr(supervisor, "_request_locked", lambda *args: {"video": {}})
    with pytest.raises(camera.CameraWorkerError):
        supervisor.stop_recording()
    report.assert_called_once_with("CAMERA_RECORD_RESULT_MISSING", component="camera", fact=True)


def test_stage_rejects_bad_target_without_production_alarm(monkeypatch):
    report = Mock()
    monkeypatch.setattr(stage, "report_alarm", report)
    with pytest.raises(stage.StageMotionError):
        stage._check_target_in_stage_limits(
            x_target=200, y_target=50, label="request",
            stage_limits={"enabled": True, "x_min": 0, "x_max": 100, "y_min": 0, "y_max": 100})
    report.assert_not_called()


def test_observed_hard_limit_and_arrival_faults_are_reported(monkeypatch):
    report = Mock()
    monkeypatch.setattr(stage, "report_alarm", report)
    with pytest.raises(stage.StageMotionError):
        stage._check_current_within_hard_limits(
            current={"x": 200, "y": 50},
            stage_limits={"enabled": True, "x_min": 0, "x_max": 100, "y_min": 0, "y_max": 100})
    assert report.call_args.args == ("MOTION_HARD_LIMIT_EXCEEDED",)
    with pytest.raises(stage.StageMotionError):
        stage._check_arrival_tolerance(err_to_target={"x": 100, "y": 0}, arrival_tolerance_pulse=10)
    assert report.call_args.args == ("MOTION_ARRIVAL_OUT_OF_TOLERANCE",)


def test_unverified_checks_are_not_marked_recovered(monkeypatch):
    recover = Mock()
    monkeypatch.setattr(stage, "recover_alarm", recover)
    stage._recover_stage_move(standstill_verified=False, tolerance_verified=False,
                              limits_verified=False, drive_verified=False)
    codes = {call.args[0] for call in recover.call_args_list}
    assert not codes & {"MOTION_HARD_LIMIT_EXCEEDED", "MOTION_DRIVE_FAULT",
                        "MOTION_STATE_UNCERTAIN", "MOTION_ARRIVAL_OUT_OF_TOLERANCE"}


def test_objective_failure_and_verified_move_recovery(monkeypatch):
    report, recover = Mock(), Mock()
    monkeypatch.setattr(objective, "report_alarm", report)
    monkeypatch.setattr(objective, "recover_alarm", recover)
    motor = SimpleNamespace(slave=4, pp_absolute_move=Mock(side_effect=[None, 0]))
    with pytest.raises(objective.ObjectiveSwitchError):
        objective._move_axis(motor, 1, 1, 1, 1)
    report.assert_called_once_with("OBJECTIVE_SWITCH_FAILED", component="objective", resource="slave:4")
    assert objective._move_axis(motor, 1, 1, 1, 1)["err_to_target_pulse"] == 0
    recover.assert_called_once_with("OBJECTIVE_SWITCH_FAILED", component="objective", resource="slave:4")


def test_background_stage_failure_remains_failed(monkeypatch):
    report = Mock()
    monkeypatch.setattr(reciprocation, "report_alarm", report)
    controller = reciprocation.StageReciprocationController()
    monkeypatch.setattr(controller, "_move_target", Mock(side_effect=stage.StageMotionError("internal detail")))
    controller._run({"targets": [{"x": 1, "y": 1}], "max_cycles": 1})
    assert controller.status()["status"] == "failed"
    assert not controller._stop_event.is_set()
    report.assert_called_once_with("STAGE_RECIPROCATION_FAILED", component="stage_reciprocation")


@pytest.mark.parametrize("error", [-51, 51, -1000, 1000])
def test_objective_excessive_error_is_reported_and_never_recovers(monkeypatch, error):
    report, recover = Mock(), Mock()
    monkeypatch.setattr(objective, "report_alarm", report)
    monkeypatch.setattr(objective, "recover_alarm", recover)
    motor = SimpleNamespace(slave=4, pp_absolute_move=Mock(return_value=error))
    with pytest.raises(objective.ObjectiveSwitchError, match="tolerance=50"):
        objective._move_axis(motor, 1000, 1, 1, 1)
    report.assert_called_once_with("OBJECTIVE_SWITCH_FAILED", component="objective", resource="slave:4")
    recover.assert_not_called()


@pytest.mark.parametrize("error", [-50, 50])
def test_objective_tolerance_boundary_allows_recovery(monkeypatch, error):
    report, recover = Mock(), Mock()
    monkeypatch.setattr(objective, "report_alarm", report)
    monkeypatch.setattr(objective, "recover_alarm", recover)
    motor = SimpleNamespace(slave=4, pp_absolute_move=Mock(return_value=error))
    assert objective._move_axis(motor, 1000, 1, 1, 1)["err_to_target_pulse"] == error
    report.assert_not_called()
    recover.assert_called_once_with("OBJECTIVE_SWITCH_FAILED", component="objective", resource="slave:4")
