"""Behavioral checks for resource sharing, cache invalidation and safe arrival."""
from types import SimpleNamespace

import pytest

from devices.motion.modbus import ModbusRTUClient
from workflow import stage_executor as stage
from workflow.scan_motion_session import ScanMotionSession, optimization_options


class FakeClient(ModbusRTUClient):
    instances = []
    fail_target = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.positions = {1: 0, 2: 0}
        self.targets = {1: 0, 2: 0}
        self.status = {1: 545, 2: 545}
        self.events = []
        self.instances.append(self)

    def connect(self):
        self.events.append("connect")
        self._connected = True
        return True

    def is_connected(self):
        return self._connected

    def disconnect(self):
        self.events.append("disconnect")
        self._connected = False

    def read_holding_registers(self, slave, address, count=1):
        return [1]  # PP mode

    def enable_motor(self, slave):
        self.events.append(("enable", slave))
        self.status[slave] = 0x427
        return True

    def _read_32bit(self, slave, address):
        return self.targets[slave] if address == self.REG_CMD_POS else self.positions[slave]

    def _read_statusword(self, slave):
        return self.status[slave]

    def _write_32bit(self, slave, address, value):
        self.events.append(("write", slave, address, value))
        if address == self.REG_TARGET_POS:
            if self.fail_target:
                return False
            self.targets[slave] = value
        return True

    def _write_controlword(self, slave, value):
        self.events.append(("control", slave, value))
        if value == 0x1F:
            self.positions[slave] = self.targets[slave]
        return True

    def _restore_enabled_state(self, slave):
        return True

    def quick_stop(self, slave):
        self.events.append(("stop", slave))
        self.status[slave] = 545
        return True


@pytest.fixture
def hardware(monkeypatch):
    FakeClient.instances = []
    FakeClient.fail_target = False
    clock = [0.0]
    monkeypatch.setattr(stage, "ModbusRTUClient", FakeClient)
    monkeypatch.setattr(stage.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(stage.time, "monotonic", lambda: clock[0])
    return clock


def move(session, **overrides):
    args = dict(port="COM3", session=session, x_target=100, y_target=200,
                profile_vel=500000, profile_acc=500000, profile_dec=500000,
                settle_s=0.5, timeout_s=1, arrival_tolerance_pulse=50)
    args.update(overrides)
    return stage.move_to_absolute(**args)


def profile_writes(client):
    return [e for e in client.events if isinstance(e, tuple) and e[0] == "write" and e[2] != client.REG_TARGET_POS]


def test_session_reuses_connection_and_profile_but_always_writes_targets(hardware):
    session = ScanMotionSession(optimization_options({}))
    move(session)
    client = session.client
    move(session, x_target=200)
    assert len(profile_writes(client)) == 6  # 3 parameters, 2 axes, first point only
    assert len([e for e in client.events if isinstance(e, tuple) and e[0] == "write" and e[2] == client.REG_TARGET_POS]) == 4
    move(session, x_target=300, profile_vel=600000, release_after_move=True)
    assert len(profile_writes(client)) == 12
    assert client.events.count("connect") == client.events.count("disconnect") == 1
    assert len([e for e in client.events if isinstance(e, tuple) and e[0] == "stop"]) == 6
    assert session.client is None
    assert session.profiles == {}


def test_profile_generation_and_connection_boundary_invalidate_cache(hardware):
    session = ScanMotionSession(optimization_options({}))
    move(session)
    client = session.client
    client.configuration_generation[1] = 1
    move(session, release_after_move=True)
    assert len(profile_writes(client)) == 9  # only invalidated axis rewritten
    move(session, release_after_move=True)
    assert len(FakeClient.instances) == 2
    assert len(profile_writes(FakeClient.instances[-1])) == 6


@pytest.mark.parametrize("option", ["reuse_connection", "reuse_profile"])
def test_reuse_can_be_disabled_independently(hardware, option):
    session = ScanMotionSession(optimization_options({"stop_shoot": {option: False}}))
    move(session)
    move(session, release_after_move=True)
    assert sum(len(profile_writes(c)) for c in FakeClient.instances) == 12
    assert len(FakeClient.instances) == (2 if option == "reuse_connection" else 1)


def test_stop_failure_still_closes_and_is_reported(hardware):
    session = ScanMotionSession(optimization_options({"stop_shoot": {"hold_enabled": True}}))
    move(session, keep_enabled=True)
    client = session.client
    client.quick_stop = lambda slave: False
    with pytest.raises(RuntimeError, match="stop returned false"):
        session.close()
    assert client.events[-1] == "disconnect"
    assert session.client is None


def test_disconnected_session_does_not_reuse_cached_profile(hardware):
    session = ScanMotionSession(optimization_options({}))
    move(session)
    client = session.client
    client._connected = False
    with pytest.raises(RuntimeError, match="connection lost"):
        move(session)
    assert len(profile_writes(client)) == 6
    assert session.profiles == {} and session.client is None


def test_failure_stops_both_axes_disconnects_and_clears_cache(hardware):
    session = ScanMotionSession(optimization_options({}))
    move(session)
    client = session.client
    FakeClient.fail_target = True
    with pytest.raises(stage.StageMotionError, match="target position"):
        move(session)
    assert ("stop", 1) in client.events and ("stop", 2) in client.events
    assert client.events[-1] == "disconnect"
    assert session.client is None and not session.profiles


def test_hold_requires_stability_and_release_stops_before_close(hardware):
    session = ScanMotionSession(optimization_options({"stop_shoot": {"hold_enabled": True}}))
    result = move(session, keep_enabled=True)
    client = session.client
    assert result["stop_shoot"]["standstill_verified"] is True
    assert result["stop_shoot"]["stable_samples"] >= 3
    assert not any(isinstance(e, tuple) and e[0] == "stop" for e in client.events)
    session.close()
    assert client.events[-3:] == [("stop", 1), ("stop", 2), "disconnect"]


@pytest.mark.parametrize("status", [None, 8])
def test_reused_snapshot_checks_fault_even_when_already_at_target(monkeypatch, hardware, status):
    reads = []

    def snapshot(*args):
        reads.append(1)
        return {"x": {"current_pos": 100, "statusword": status}, "y": {"current_pos": 200, "statusword": 4}}

    monkeypatch.setattr(stage, "snapshot_xy", snapshot)
    with pytest.raises(stage.StageMotionError):
        stage._wait_xy_arrival(x_motor=None, y_motor=None, x_target=100, y_target=200,
                              timeout_s=1, poll_s=0.05, monitor_tolerance=50,
                              stage_limits=None, reuse_status=True)
    assert len(reads) == 1


def test_reused_snapshot_does_not_read_status_again(monkeypatch, hardware):
    rows = iter([0, 100])
    calls = []

    def snapshot(*args):
        pos = next(rows)
        calls.append(pos)
        return {a: {"current_pos": pos, "statusword": 4} for a in ("x", "y")}

    monkeypatch.setattr(stage, "snapshot_xy", snapshot)
    monkeypatch.setattr(stage, "_check_axis_fault", lambda *args: pytest.fail("duplicate status read"))
    result = stage._wait_xy_arrival(x_motor=None, y_motor=None, x_target=100, y_target=100,
                                   timeout_s=1, poll_s=0.05, monitor_tolerance=0,
                                   stage_limits=None, reuse_status=True)
    assert result["current"] == {"x": 100, "y": 100}
    assert calls == [0, 100]


@pytest.mark.parametrize("scenario", ["no_target_bit", "drift", "cancel"])
def test_hold_does_not_accept_position_tolerance_alone(monkeypatch, hardware, scenario):
    index = [0]

    def snapshot(*args):
        index[0] += 1
        pos = (0 if index[0] % 2 else 20) if scenario == "drift" else 0
        status = 4 if scenario == "no_target_bit" else 0x404
        return {a: {"current_pos": pos, "statusword": status} for a in ("x", "y")}

    monkeypatch.setattr(stage, "snapshot_xy", snapshot)
    stopped = []
    monkeypatch.setattr(stage, "_quick_stop_xy", lambda *args: stopped.append(True))
    args = dict(x_motor=None, y_motor=None, x_target=0, y_target=0, timeout_s=0.4,
                poll_s=0.05, monitor_tolerance=50, stage_limits=None, reuse_status=True,
                require_standstill=True)
    if scenario == "cancel":
        args["stop_event"] = SimpleNamespace(is_set=lambda: True)
        assert stage._wait_xy_arrival(**args)["stopped_by_request"] is True
        assert stopped == [True]
    else:
        with pytest.raises(stage.StageMotionError, match="timed out"):
            stage._wait_xy_arrival(**args)


@pytest.mark.parametrize("failure", [None, "capture", "autofocus", "cancel"])
def test_scan_yields_serial_to_autofocus_and_cleans_up(tmp_path, monkeypatch, hardware, failure):
    from workflow import scan_executor as scan
    from workflow.task_control import TaskCanceled
    import json

    monkeypatch.setattr(scan, "move_to_absolute", stage.move_to_absolute)
    monkeypatch.setattr(scan, "_check_motion_guard", lambda *args: None)
    monkeypatch.setattr(scan, "open_camera", lambda **kwargs: object())
    monkeypatch.setattr(scan, "close_camera", lambda cam: None)
    captured = []
    error = RuntimeError("original failure")

    def autofocus(**kwargs):
        assert FakeClient.instances[-1].events[-1] == "disconnect"
        assert not FakeClient.instances[-1].is_connected()
        if failure == "autofocus":
            raise error
        return {"status": "success"}

    def capture(**kwargs):
        captured.append(True)
        if len(captured) == 2 and failure == "capture":
            raise error
        return {"saved_path": "fake.bmp"}

    def cancel(params, phase):
        if failure == "cancel" and phase == "after_capture:point_2":
            raise TaskCanceled("requested")

    monkeypatch.setattr(scan, "_execute_autofocus_before_capture", autofocus)
    monkeypatch.setattr(scan, "capture_with_opened_camera", capture)
    monkeypatch.setattr(scan, "raise_if_cancel_requested", cancel)
    path = tmp_path / "scan.json"
    params = dict(task_id="test", task_type="capture", plate_type="24-well", well_name="B2",
                  objective_name="4x", settle_s=0.5, scan_output_json=str(path), save_dir=str(tmp_path),
                  filename_pattern="{index}.bmp", device_index=0,
                  autofocus_decision={"should_run": True},
                  autofocus_cfg={"trigger": {"scope": "once_per_task"}},
                  motion={"port": "COM3", "stop_shoot": {"hold_enabled": True}})
    points = [dict(index=i, row_index=0, col_index=i, view_down_mm=0, view_right_mm=i,
                   stage_x_target=i*100, stage_y_target=i*100) for i in (1, 2, 3)]
    plan = dict(points=points, reference={}, scan_config={})
    if failure:
        with pytest.raises(TaskCanceled if failure == "cancel" else RuntimeError) as caught:
            scan.execute_scan_capture({"plate": {}}, params, plan)
        if failure != "cancel":
            assert caught.value is error
    else:
        scan.execute_scan_capture({"plate": {}}, params, plan)
    assert all(not c.is_connected() and c.events.count("disconnect") == 1 for c in FakeClient.instances)
    assert len(FakeClient.instances) == (1 if failure == "autofocus" else 2)
    last = FakeClient.instances[-1]
    assert ("stop", 1) in last.events and ("stop", 2) in last.events
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == ("success" if failure is None else "canceled" if failure == "cancel" else "failed")
    if failure in ("capture", "cancel"):
        assert result["motion_session_cleanup"]["lifecycle_counts"]["disconnect"] == 1


@pytest.mark.parametrize("raw", [{"enabled": "false"}, {"hold_enabled": True, "reuse_connection": False}, {"typo": True}, None])
def test_options_reject_ambiguous_or_unsafe_values(raw):
    with pytest.raises(ValueError):
        optimization_options({"stop_shoot": raw})


def test_mode_reset_and_retry_invalidate_real_client_generation(monkeypatch):
    from pymodbus.exceptions import ModbusIOException
    from devices.motion.communication_timing import collect
    monkeypatch.setattr("devices.motion.modbus.time.sleep", lambda seconds: None)
    client = ModbusRTUClient()
    client._connected = True
    replies = iter([ModbusIOException("No response received"), SimpleNamespace(isError=lambda: False)])

    def write(**kwargs):
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return value

    client._client = SimpleNamespace(socket=None, write_register=write)
    with collect({}):
        assert client.write_register(1, client.REG_MODE_SELECT, 1)
    assert client.configuration_generation[1] == 2  # mode write + failed attempt
    client._client.write_register = lambda **kwargs: SimpleNamespace(isError=lambda: False)
    assert client.write_register(1, client.REG_CONTROLWORD, client.CMD_FAULT_RESET)
    assert client.configuration_generation[1] == 3
