from __future__ import annotations

import threading
import math

import pytest

from workflow import hardware_guard
from workflow.stage_reciprocation import StageReciprocationController


class _FakeClient:
    REG_CURRENT_POS = 968
    REG_CMD_POS = 966
    REG_TARGET_POS = 999
    REG_PROFILE_VEL_HIGH = 1016
    REG_PROFILE_ACC_HIGH = 1020
    REG_PROFILE_DEC_HIGH = 1022
    CMD_ENABLE_OPERATION = 0x0F
    STAT_FAULT = 0x0008

    last_instance = None
    reach_target_on_trigger = True
    quick_stop_behaviors = {}
    quick_stop_hook = None

    def __init__(self, *args, **kwargs) -> None:
        self.positions = {1: 0, 2: 0}
        self.targets = {1: 0, 2: 0}
        self.quick_stops = []
        type(self).last_instance = self

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
        if type(self).reach_target_on_trigger and int(value) == (self.CMD_ENABLE_OPERATION | 0x10):
            self.positions[slave] = self.targets[slave]
        return True

    def _restore_enabled_state(self, _slave):
        return True

    def quick_stop(self, slave):
        self.quick_stops.append(slave)
        hook = type(self).quick_stop_hook
        if hook is not None:
            hook(self, slave)
        behavior = type(self).quick_stop_behaviors.get(slave, True)
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior


class _FakeMotor:
    MODE_PROFILE_POSITION = 0x01

    def __init__(self, client, slave) -> None:
        self.client = client
        self.slave = slave

    def _ensure_mode_and_enable(self, target_mode, auto_enable=True):
        return target_mode == self.MODE_PROFILE_POSITION and auto_enable is True


def _install_fake_motion(monkeypatch, stage_executor, *, reaches_target: bool) -> None:
    _FakeClient.last_instance = None
    _FakeClient.reach_target_on_trigger = reaches_target
    _FakeClient.quick_stop_behaviors = {}
    _FakeClient.quick_stop_hook = None
    monkeypatch.setattr(stage_executor, "ModbusRTUClient", _FakeClient)
    monkeypatch.setattr(stage_executor, "MotorManager", _FakeMotor)


def _move(stage_executor, **overrides):
    args = {
        "port": "COM3",
        "x_target": 100,
        "y_target": 200,
        "profile_vel": 10,
        "profile_acc": 20,
        "profile_dec": 30,
        "settle_s": 0,
        "timeout_s": 1,
        "poll_s": 0.1,
        "arrival_tolerance_pulse": 0,
    }
    args.update(overrides)
    return stage_executor.move_to_absolute(**args)


def test_precommand_cancellation_does_not_open_motion_bus(monkeypatch) -> None:
    from workflow import stage_executor

    stop_event = threading.Event()
    stop_event.set()

    class ShouldNotOpenClient:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("pre-canceled move must not open the motion bus")

    monkeypatch.setattr(stage_executor, "ModbusRTUClient", ShouldNotOpenClient)

    result = _move(stage_executor, stop_event=stop_event)

    assert result["stopped_by_request"] is True
    assert result["before"]["x"]["current_pos"] is None
    assert result["after"]["y"]["current_pos"] is None


def test_arrival_cancellation_quick_stops_both_axes(monkeypatch) -> None:
    from workflow import stage_executor

    _install_fake_motion(monkeypatch, stage_executor, reaches_target=False)
    stop_event = threading.Event()

    result = _move(
        stage_executor,
        stop_event=stop_event,
        progress_callback=lambda _current: stop_event.set(),
    )

    assert result["stopped_by_request"] is True
    assert _FakeClient.last_instance.quick_stops == [1, 2]


def test_settle_cancellation_quick_stops_both_axes_again(monkeypatch) -> None:
    from workflow import stage_executor

    _install_fake_motion(monkeypatch, stage_executor, reaches_target=True)
    stop_event = threading.Event()

    def request_stop_after_normal_arrival(client, _slave) -> None:
        if client.quick_stops == [1, 2]:
            stop_event.set()

    _FakeClient.quick_stop_hook = request_stop_after_normal_arrival

    result = _move(stage_executor, stop_event=stop_event, settle_s=1.0)

    assert result["stopped_by_request"] is True
    assert _FakeClient.last_instance.quick_stops == [1, 2, 1, 2]


def test_interruptible_wait_uses_at_most_fifty_millisecond_slices(monkeypatch) -> None:
    from workflow import stage_executor

    stop_event = threading.Event()
    sleeps = []

    def fake_sleep(duration_s: float) -> None:
        sleeps.append(duration_s)
        stop_event.set()

    monkeypatch.setattr(stage_executor.time, "sleep", fake_sleep)

    assert stage_executor._wait_interruptibly(stop_event, 1.0) is True
    assert sleeps
    assert max(sleeps) <= 0.05


@pytest.mark.parametrize("field", ["settle_s", "timeout_s", "poll_s"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_motion_timing_is_rejected_before_open(monkeypatch, field, value) -> None:
    from workflow import stage_executor

    class ShouldNotOpenClient:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("invalid motion timing must be rejected before opening the bus")

    monkeypatch.setattr(stage_executor, "ModbusRTUClient", ShouldNotOpenClient)
    with pytest.raises(stage_executor.StageMotionError, match="finite"):
        _move(stage_executor, **{field: value})


def test_quick_stop_failure_latches_hardware_and_raises_distinct_error(monkeypatch) -> None:
    from workflow import hardware_guard, stage_executor

    hardware_guard._reset_hardware_guard_for_tests()
    _install_fake_motion(monkeypatch, stage_executor, reaches_target=False)
    _FakeClient.quick_stop_behaviors = {
        1: False,
        2: RuntimeError("serial write failed"),
    }
    stop_event = threading.Event()

    try:
        with pytest.raises(stage_executor.StageMotionStopFailed) as exc_info:
            _move(
                stage_executor,
                stop_event=stop_event,
                progress_callback=lambda _current: stop_event.set(),
            )

        assert "x axis slave=1 returned false" in str(exc_info.value)
        assert "y axis slave=2 raised RuntimeError" in str(exc_info.value)
        assert _FakeClient.last_instance.quick_stops == [1, 2]
        safety = hardware_guard.hardware_safety_status()
        assert safety["safe"] is False
        assert safety["error_code"] == "STAGE_QUICK_STOP_FAILED"
    finally:
        hardware_guard._reset_hardware_guard_for_tests()


def test_ambiguous_stage_trigger_latches_unknown_motion_state(monkeypatch) -> None:
    from workflow import stage_executor

    hardware_guard._reset_hardware_guard_for_tests()
    _install_fake_motion(monkeypatch, stage_executor, reaches_target=True)
    original_write_controlword = _FakeClient._write_controlword

    def lose_x_trigger_ack(self, slave, value):
        if slave == 1 and int(value) == (self.CMD_ENABLE_OPERATION | 0x10):
            return False
        return original_write_controlword(self, slave, value)

    monkeypatch.setattr(_FakeClient, "_write_controlword", lose_x_trigger_ack)
    try:
        with pytest.raises(stage_executor.StageMotionStopFailed, match="X 轴运动触发结果未知"):
            _move(stage_executor)

        assert _FakeClient.last_instance.quick_stops == [1, 2]
        safety = hardware_guard.hardware_safety_status()
        assert safety["safe"] is False
        assert safety["error_code"] == "STAGE_MOTION_STATE_UNKNOWN"
    finally:
        hardware_guard._reset_hardware_guard_for_tests()


def test_stage_stop_does_not_relabel_existing_failure_as_stopped() -> None:
    from workflow.stage_reciprocation import StageReciprocationController

    controller = StageReciprocationController()
    controller._status = {
        **controller._status,
        "status": "failed",
        "message": "XY quick stop could not be confirmed",
        "error": "XY quick stop could not be confirmed",
    }

    result = controller.stop()

    assert result["status"] == "failed"
    assert result["error"] == "XY quick stop could not be confirmed"


def test_safety_latch_cancels_admitted_stage_before_thread_start(monkeypatch) -> None:
    from workflow import stage_reciprocation

    hardware_guard._reset_hardware_guard_for_tests()
    controller = StageReciprocationController()
    monkeypatch.setattr(stage_reciprocation, "stage_reciprocation_controller", controller)
    hardware_guard.acquire_hardware_operation("stage_reciprocation", "stage-before-latch")
    try:
        hardware_guard.latch_hardware_unsafe(
            "INJECTED_UNSAFE",
            "injected unsafe state between admission and physical start",
        )

        assert controller._stop_event.is_set()
        with pytest.raises(hardware_guard.HardwareGuardError) as exc_info:
            controller.start({})
        assert exc_info.value.error_code == "HARDWARE_SAFETY_LATCHED"
        assert controller._thread is None
    finally:
        hardware_guard._reset_hardware_guard_for_tests()
