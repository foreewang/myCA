"""Offline checks: never open a serial port, invoke systemd or access hardware."""
from pathlib import Path
import json
import logging
from types import SimpleNamespace

import pytest
from pymodbus.register_read_message import ReadHoldingRegistersRequest
from pymodbus.register_write_message import WriteSingleRegisterRequest

from devices.motion import communication_timing, modbus
from tools import diagnose_stage_serial as diagnostic


@pytest.fixture(autouse=True)
def restore_diagnostic_logger_levels():
    """The CLI quiets driver logs; do not leak that process state to other tests."""
    loggers = [logging.getLogger(name) for name in ("devices.motion.modbus", "pymodbus")]
    levels = [logger.level for logger in loggers]
    try:
        yield
    finally:
        for logger, level in zip(loggers, levels):
            logger.setLevel(level)


class SimulatedSerial:
    """Only the transport is fake; use the real wrapper, requests and timing."""

    outcomes = []
    instances = []
    open_ok = True

    def __init__(self, **kwargs):
        self.socket = SimpleNamespace(is_open=False)
        self.requests = []
        self.sent = []
        self.pending = list(type(self).outcomes)
        type(self).instances.append(self)

    def connect(self):
        self.socket.is_open = type(self).open_ok
        return self.socket.is_open

    def close(self):
        self.socket.is_open = False

    def send(self, request):
        self.sent.append(request)
        return len(request)

    def recv(self, size):
        return b"\x01\x03"

    def read_holding_registers(self, *, address, count, slave):
        return self.execute(ReadHoldingRegistersRequest(address, count, slave=slave))

    def execute(self, request=None):
        self.requests.append(request)
        self.connect()  # Models the library's implicit reconnect in execute().
        self.send(b"\x01\x03\x03\xc8")
        self.recv(2)
        outcome = self.pending.pop(0) if self.pending else "success"
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "timeout":
            self.close()  # Models a no-response transaction's library close.
            return FakeError("No response received, 0 received")
        if outcome == "failure":
            return FakeError("Illegal function")
        values = [0, 123] if request.count == 2 else [545]
        return SimpleNamespace(isError=lambda: False, registers=values)


class FakeError:
    def __init__(self, message):
        self.message = message

    def isError(self):
        return True

    def __str__(self):
        return self.message


@pytest.fixture
def simulated_transport(monkeypatch):
    SimulatedSerial.outcomes = []
    SimulatedSerial.instances = []
    SimulatedSerial.open_ok = True
    monkeypatch.setattr(modbus, "ModbusSerialClient", SimulatedSerial)
    monkeypatch.setattr(modbus.time, "sleep", lambda seconds: None)
    return SimulatedSerial


@pytest.mark.parametrize("modbus_request", [
    WriteSingleRegisterRequest(896, 15, slave=1),
    ReadHoldingRegistersRequest(999, 2, slave=1),
    ReadHoldingRegistersRequest(968, 1, slave=1),
    ReadHoldingRegistersRequest(897, 2, slave=1),
    ReadHoldingRegistersRequest(968, 2, slave=3),
    None,
])
def test_non_allowlisted_request_never_reaches_transport(modbus_request):
    probe = diagnostic.TransportProbe()
    guarded = diagnostic.observed_serial_class(SimulatedSerial, probe, (1, 2))()
    with pytest.raises(ValueError, match="non-allowlisted"):
        guarded.execute(modbus_request)
    assert guarded.requests == []
    assert guarded.sent == []
    assert not guarded.socket.is_open


@pytest.mark.parametrize("slave,address,count", [(1, 968, 2), (2, 968, 2), (1, 897, 1), (2, 897, 1)])
def test_known_read_only_requests_pass(slave, address, count, simulated_transport):
    probe = diagnostic.TransportProbe()
    guarded = diagnostic.observed_serial_class(simulated_transport, probe, (1, 2))()
    result = guarded.execute(ReadHoldingRegistersRequest(address, count, slave=slave))
    assert len(result.registers) == count
    assert len(guarded.requests) == 1


class Clock:
    def __init__(self, start):
        self.value = start
        self.sleeps = []

    def time(self):
        return self.value

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.value += duration


@pytest.mark.parametrize("previous,slave,end,start,gap,expected", [
    (1, 1, 1.0, 1.0, 10, 0),
    (None, 1, None, 1.0, 10, 0),
    (1, 2, 1.0, 1.002, 10, 8),
    (1, 2, 1.0, 1.011, 10, 0),
    (1, 2, 1.0, 1.0, 0, 0),
])
def test_minimum_gap_only_adds_missing_switch_wait(previous, slave, end, start, gap, expected):
    clock = Clock(start)
    waited = diagnostic.wait_for_gap(previous, slave, end, gap, clock.time, clock.sleep)
    assert waited == pytest.approx(expected)
    assert sum(clock.sleeps) * 1000 == pytest.approx(expected)


def run_case():
    return diagnostic.run_case(
        modbus, communication_timing, pattern=[(1, 968, 2), (2, 968, 2)],
        gap_ms=0, reads=10, seconds=20, port="fake-serial-no-hardware",
        baudrate=115200, slaves=(1, 2),
    )


def test_real_wrapper_counts_implicit_reopen_and_preserves_read_only(simulated_transport):
    simulated_transport.outcomes = ["success", "timeout", "success"]
    result = run_case()
    assert result["status"] == "complete"
    assert result["completed_reads"] == 10
    assert result["communication"]["retry_count"] == 1
    assert result["communication"]["switched_retry_count"] == 1
    assert result["communication"]["failed_transaction_count"] == 0
    assert result["samples"][1]["serial_reopens"] == 1
    assert result["transport"]["counts"]["serial_open"] == 2
    assert result["transport"]["counts"]["serial_close"] == 2
    assert len(result["transport_failures"]) == 1
    assert "No response" in result["communication"]["slow_or_retried"][0]["attempts"][0]["error"]
    client = simulated_transport.instances[-1]
    assert all(r.function_code == 3 and (r.address, r.count) in diagnostic.ALLOWED_READS for r in client.requests)
    assert not client.socket.is_open
    assert modbus.ModbusSerialClient is simulated_transport


def test_three_failed_transactions_stop_and_close(simulated_transport):
    simulated_transport.outcomes = ["failure"] * 10
    result = run_case()
    assert result["status"] == "three_consecutive_failures"
    assert result["completed_reads"] == 3
    assert result["communication"]["failed_transaction_count"] == 3
    assert result["transport"]["counts"]["serial_close"] == 1
    assert not simulated_transport.instances[-1].socket.is_open


@pytest.mark.parametrize("error,status", [(RuntimeError("fake I/O failure"), "error"), (KeyboardInterrupt(), "interrupted")])
def test_exception_and_interrupt_still_disconnect(error, status, simulated_transport):
    simulated_transport.outcomes = [error]
    result = run_case()
    assert result["status"] == status
    assert result["completed_reads"] == 0
    assert result["transport"]["counts"]["serial_close"] == 1
    assert not simulated_transport.instances[-1].socket.is_open
    assert modbus.ModbusSerialClient is simulated_transport


def test_failed_connect_returns_error_without_reading(simulated_transport):
    simulated_transport.open_ok = False
    result = run_case()
    assert result["status"] == "error"
    assert "Cannot open serial port" in result["error"]
    assert result["completed_reads"] == 0
    assert result["transport"]["counts"]["serial_open_failed"] == 1
    assert simulated_transport.instances[-1].requests == []


def mock_idle_checks(monkeypatch, tmp_path, *, active="inactive", main_pid="0", control_pid="0", owner_code=1, owner_stdout="", owner_stderr=""):
    root = tmp_path.resolve()
    port = root / "fake-tty"
    port.touch()
    monkeypatch.setattr(Path, "is_char_device", lambda path: path == port)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "systemctl":
            return SimpleNamespace(stdout=f"LoadState=loaded\nActiveState={active}\nWorkingDirectory={root}\nMainPID={main_pid}\nControlPID={control_pid}\n", returncode=0)
        assert command == ["fuser", "-v", str(port)]
        return SimpleNamespace(returncode=owner_code, stdout=owner_stdout, stderr=owner_stderr)

    monkeypatch.setattr(diagnostic.subprocess, "run", fake_run)
    return root, port, calls


def test_idle_requires_stopped_service_and_unused_port(monkeypatch, tmp_path):
    root, port, calls = mock_idle_checks(monkeypatch, tmp_path)
    fields = diagnostic.require_idle(root, "colony-system-api", port)
    assert fields["ActiveState"] == "inactive"
    assert [call[0] for call in calls] == ["systemctl", "fuser"]


@pytest.mark.parametrize("fields", [{"active": "active"}, {"main_pid": "123"}, {"control_pid": "456"}])
def test_active_or_live_service_is_rejected(monkeypatch, tmp_path, fields):
    root, port, calls = mock_idle_checks(monkeypatch, tmp_path, **fields)
    with pytest.raises(ValueError, match="Service must exist and be stopped"):
        diagnostic.require_idle(root, "colony-system-api", port)
    assert len(calls) == 1


@pytest.mark.parametrize("fields,message", [
    ({"owner_code": 0, "owner_stdout": "1234"}, "in use"),
    ({"owner_code": 1, "owner_stdout": "1234"}, "in use"),
    ({"owner_code": 1, "owner_stderr": "permission denied"}, "Cannot verify"),
    ({"owner_code": 2}, "Cannot verify"),
])
def test_occupied_or_unverifiable_port_is_rejected(monkeypatch, tmp_path, fields, message):
    root, port, _ = mock_idle_checks(monkeypatch, tmp_path, **fields)
    with pytest.raises(ValueError, match=message):
        diagnostic.require_idle(root, "colony-system-api", port)


def test_case_order_brackets_gaps_with_baselines_and_reverses_second_round():
    first = diagnostic.cases_for_round(1, 1, 2)
    second = diagnostic.cases_for_round(2, 1, 2)
    assert [name for name, _, _ in first][-5:] == ["alternate_0ms_before", "alternate_2ms", "alternate_5ms", "alternate_10ms", "alternate_0ms_after"]
    assert [gap for name, _, gap in second if name.startswith("alternate_")] == [0, 10, 5, 2, 0]
    assert all((address, count) in diagnostic.ALLOWED_READS for _, pattern, _ in first + second for _, address, count in pattern)


def test_retry_rates_exclude_unfinished_call(simulated_transport):
    simulated_transport.outcomes = ["success", "timeout", KeyboardInterrupt()]
    result = run_case()
    assert result["communication"]["retry_count"] == 1
    assert result["completed_reads"] == 1
    assert result["retry_attempts_per_1000_reads"] == 0
    assert result["retries_per_1000_switches"] is None
    assert result["unfinished_read"]["slave"] == 2
    assert result["unfinished_read"]["retry_count"] == 1


def prepare_simulated_main(monkeypatch, tmp_path):
    root = tmp_path / "project"
    motion = root / "devices" / "motion"
    motion.mkdir(parents=True)
    (motion / "modbus.py").write_text("# source hash placeholder\n")
    (motion / "communication_timing.py").write_text("# source hash placeholder\n")
    port = tmp_path / "fake-tty"
    port.touch()
    output = tmp_path / "report.json"
    monkeypatch.setattr(diagnostic.sys, "platform", "linux")
    monkeypatch.setattr(diagnostic.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(diagnostic, "require_idle", lambda *args: {})
    monkeypatch.setattr(diagnostic.sys, "path", list(diagnostic.sys.path))
    monkeypatch.setattr(diagnostic.sys, "argv", [
        "diagnose_stage_serial.py", "--root", str(root), "--port", str(port),
        "--output", str(output), "--rounds", "1", "--reads", "4"])
    return output


def test_main_saves_partial_result_after_interrupt(monkeypatch, tmp_path, simulated_transport):
    output = prepare_simulated_main(monkeypatch, tmp_path)
    simulated_transport.outcomes = ["success", KeyboardInterrupt()]
    assert diagnostic.main() == 130
    report = json.loads(output.read_text())
    assert report["status"] == "interrupted"
    assert "finished_utc" in report
    assert len(report["cases"]) == 1
    assert report["cases"][0]["completed_reads"] == 1
    assert report["cases"][0]["transport"]["counts"]["serial_close"] == 1
    assert not output.with_name(output.name + ".tmp").exists()


def test_existing_report_is_not_overwritten_or_serial_opened(monkeypatch, tmp_path, simulated_transport):
    output = prepare_simulated_main(monkeypatch, tmp_path)
    output.write_text("existing result")
    with pytest.raises(FileExistsError):
        diagnostic.main()
    assert output.read_text() == "existing result"
    assert simulated_transport.instances == []
