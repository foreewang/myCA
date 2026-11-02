"""Simulate finish-stage displacement without connecting to any hardware."""
import json
import pytest

from devices.motion.modbus import ModbusRTUClient
from workflow import stage_executor as stage


class FakeClient(ModbusRTUClient):
    instances = []
    displacement_phase = None
    fail_diagnostic_read = False

    def __init__(self, **kwargs):
        self.positions = {1: 0, 2: 0}
        self.targets = {1: 0, 2: 0}
        self.controls = {1: 0x0F, 2: 0x0F}
        self.writes = []
        self.target_reads = 0
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def _write_32bit(self, slave, reg, value):
        self.writes.append((slave, reg, value))
        if reg == self.REG_TARGET_POS:
            self.targets[slave] = value
        return True

    def _write_controlword(self, slave, value):
        self.writes.append((slave, self.REG_CONTROLWORD, value))
        self.controls[slave] = value
        if value == 0x1F:
            self.positions[slave] = self.targets[slave]
        return True

    def _read_32bit(self, slave, reg):
        if reg == self.REG_TARGET_POS:
            self.target_reads += 1
            if self.fail_diagnostic_read:
                raise OSError("simulated diagnostic read failure")
            return self.targets[slave]
        return self.positions[slave]

    def _read_statusword(self, slave):
        return 545 if self.controls[slave] == 6 else 0x427

    def read_holding_registers(self, slave, address, count=1):
        assert address == self.REG_CONTROLWORD
        return [self.controls[slave]]

    def _restore_enabled_state(self, slave):
        self._write_controlword(slave, 0x0F)
        if slave == 1 and self.displacement_phase == "restore":
            self.positions[slave] -= 80000
        return True

    def quick_stop(self, slave):
        self._write_controlword(slave, 6)
        if slave == 1 and self.displacement_phase == "stop":
            self.positions[slave] -= 80000
        return True


@pytest.fixture
def hardware(monkeypatch):
    FakeClient.instances = []
    FakeClient.displacement_phase = None
    FakeClient.fail_diagnostic_read = False
    monkeypatch.setattr(stage, "ModbusRTUClient", FakeClient)
    class FakeMotor:
        MODE_PROFILE_POSITION = 1

        def __init__(self, client, slave):
            self.client, self.slave = client, slave

        def _ensure_mode_and_enable(self, *args):
            return True

    monkeypatch.setattr(stage, "MotorManager", FakeMotor)
    clock = [0.0]
    monkeypatch.setattr(stage.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(stage.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))


def move(**kwargs):
    args = dict(
        port="fake", x_target=4225601, y_target=5507169,
        profile_vel=500000, profile_acc=500000, profile_dec=500000,
        settle_s=0, arrival_tolerance_pulse=3000,
    )
    args.update(kwargs)
    return stage.move_to_absolute(**args)


def samples(caplog):
    prefix = "event=stage_finish_probe data="
    return [json.loads(r.getMessage()[len(prefix):]) for r in caplog.records
            if r.getMessage().startswith(prefix)]


@pytest.mark.parametrize("phase", ["restore", "stop"])
def test_displacement_is_logged_at_its_first_phase_before_arrival_failure(hardware, caplog, phase):
    FakeClient.displacement_phase = phase
    with pytest.raises(stage.StageMotionError, match="err=-80000"):
        move(finish_diagnostics=True)
    records = samples(caplog)
    assert len(records) == 8
    assert len({r["move_id"] for r in records}) == 1
    x = {r["phase"]: r for r in records if r["axis"] == "x"}
    assert x["arrival_before_restore"]["current_pos"] == 4225601
    assert x["after_restore_before_stop"]["current_pos"] == (4145601 if phase == "restore" else 4225601)
    assert x["after_stop_return"]["current_pos"] == 4145601
    assert x["after_stop_return"]["statusword"] == 545
    assert x["after_stop_return"]["controlword"] == 6
    assert x["after_settle"]["current_pos"] == 4145601
    assert all(r["target_register"] == 4225601 and not r["errors"] for r in x.values())
    assert all(r["current_pos"] == 5507169 for r in records if r["axis"] == "y")
    assert all(r["start_ms"] <= r["end_ms"] for r in records)
    # The normal error path still stops BOTH axes after validation fails.
    assert FakeClient.instances[-1].writes[-2:] == [(1, 896, 6), (2, 896, 6)]


def test_disabled_has_no_extra_target_reads_and_enabled_preserves_writes(hardware, caplog):
    move()
    baseline = FakeClient.instances[-1]
    assert baseline.target_reads == 0
    assert not samples(caplog)
    move(finish_diagnostics=True)
    observed = FakeClient.instances[-1]
    assert observed.writes == baseline.writes
    assert observed.target_reads == 8


def test_diagnostic_read_failure_does_not_prevent_stopping(hardware, caplog):
    FakeClient.fail_diagnostic_read = True
    result = move(finish_diagnostics=True)
    assert result["err_to_target"] == {"x": 0, "y": 0}
    assert FakeClient.instances[-1].writes[-2:] == [(2, 896, 15), (2, 896, 6)]
    records = samples(caplog)
    assert len(records) == 8
    assert all("OSError" in r["errors"]["target_register"] for r in records)


@pytest.mark.parametrize("enabled", [False, True])
def test_approach_propagates_diagnostics_to_each_leg(monkeypatch, enabled):
    calls = []
    cancel_check = lambda: None
    monkeypatch.setattr(stage, "move_to_absolute", lambda **kw: calls.append(kw) or {})
    stage.move_to_absolute_with_approach(
        port="fake", x_target=100, y_target=200,
        profile_vel=1, profile_acc=1, profile_dec=1,
        approach_cfg={"enabled": enabled, "margin_pulse": 10},
        finish_diagnostics=True, keep_enabled=True, cancel_check=cancel_check,
    )
    assert len(calls) == (2 if enabled else 1)
    assert all(c["finish_diagnostics"] is True for c in calls)
    assert all(c["keep_enabled"] is True and c["cancel_check"] is cancel_check for c in calls)


@pytest.mark.parametrize("env,expected", [(None, True), ("0", False)])
def test_compensation_enables_diagnostics_with_environment_opt_out(monkeypatch, env, expected):
    from workflow import compensate_executor as compensate
    if env is None:
        monkeypatch.delenv("COLONY_COMPENSATE_FINISH_DIAGNOSTICS", raising=False)
    else:
        monkeypatch.setenv("COLONY_COMPENSATE_FINISH_DIAGNOSTICS", env)
    calls = []
    monkeypatch.setattr(compensate, "move_to_absolute_with_approach", lambda **kw: calls.append(kw) or {})
    compensate._move_to_compensate_target(ctx={"plate": {}}, params={}, target_x=100, target_y=200)
    assert calls[0]["finish_diagnostics"] is expected
    assert calls[0]["keep_enabled"] is True
    assert callable(calls[0]["cancel_check"])


def test_compensation_holds_after_standstill_without_triggering_shutdown_displacement(hardware, monkeypatch, caplog):
    from workflow import compensate_executor as compensate
    FakeClient.displacement_phase = "stop"
    monkeypatch.delenv("COLONY_COMPENSATE_FINISH_DIAGNOSTICS", raising=False)
    result = compensate._move_to_compensate_target(
        ctx={"plate": {"runtime_guard": {"enabled": True}}},
        params={}, target_x=4225601, target_y=5507169,
    )
    client = FakeClient.instances[-1]
    assert result["err_to_target"] == {"x": 0, "y": 0}
    assert result["stop_shoot"]["held_enabled"] is True
    assert result["stop_shoot"]["standstill_verified"] is True
    assert result["stop_shoot"]["stable_samples"] >= 3
    assert client.controls == {1: 15, 2: 15}
    assert not any(reg == 896 and value == 6 for slave, reg, value in client.writes)
    records = samples(caplog)
    assert len(records) == 6
    assert {r["phase"] for r in records} == {
        "arrival_before_restore", "after_restore_hold_enabled", "after_settle",
    }
    assert all(r["controlword"] == 15 for r in records if r["phase"] == "after_settle")


def test_hold_still_rejects_displacement_after_restore_and_stops_both_axes(hardware):
    FakeClient.displacement_phase = "restore"
    with pytest.raises(stage.StageMotionError, match="position/status changed"):
        move(keep_enabled=True)
    assert FakeClient.instances[-1].writes[-2:] == [(1, 896, 6), (2, 896, 6)]


@pytest.mark.parametrize("bad_status", [None, 8])
def test_hold_final_missing_status_or_fault_still_stops_both_axes(hardware, monkeypatch, bad_status):
    original = FakeClient._read_statusword

    def read_status(client, slave):
        if client.controls[slave] == 15 and (slave, 896, 31) in client.writes:
            return bad_status
        return original(client, slave)

    monkeypatch.setattr(FakeClient, "_read_statusword", read_status)
    with pytest.raises(stage.StageMotionError, match="statusword|fault"):
        move(keep_enabled=True)
    assert FakeClient.instances[-1].writes[-2:] == [(1, 896, 6), (2, 896, 6)]


def test_hold_position_in_tolerance_without_target_reached_times_out_and_stops(hardware, monkeypatch):
    monkeypatch.setattr(FakeClient, "_read_statusword", lambda *args: 4)
    with pytest.raises(stage.StageMotionError, match="timed out"):
        move(keep_enabled=True, timeout_s=0.4)
    assert FakeClient.instances[-1].writes[-2:] == [(1, 896, 6), (2, 896, 6)]


@pytest.mark.parametrize("phase", ["moving", "after_restore"])
def test_compensation_cancellation_inside_motion_stops_both_axes(hardware, phase):
    from workflow import compensate_executor as compensate
    from workflow.task_control import TaskCanceled

    def canceled():
        if not FakeClient.instances:
            return False
        client = FakeClient.instances[-1]
        if (1, 896, 31) not in client.writes:
            return False
        return phase == "moving" or client.controls[1] == 15

    with pytest.raises(TaskCanceled):
        compensate._move_to_compensate_target(
            ctx={"plate": {}}, params={"_cancel_check": canceled}, target_x=100, target_y=200,
        )
    assert FakeClient.instances[-1].writes[-2:] == [(1, 896, 6), (2, 896, 6)]
