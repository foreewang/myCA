"""Passive diagnostics must preserve bus calls, waits, errors and task isolation."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from devices.motion import communication_timing as timing
from devices.motion.modbus import ModbusRTUClient


class Reply:
    registers = [545]

    def __init__(self, error=False):
        self.error = error

    def isError(self):
        return self.error

    def __str__(self):
        return "No response received" if self.error else "ok"


def test_diagnostics_preserve_transactions_and_retry_sleep(monkeypatch):
    traces = []
    metrics = {}
    for enabled in (False, True):
        trace = []
        ticks = [0.0]
        monkeypatch.setattr("devices.motion.modbus.time.perf_counter", lambda: ticks[0])
        monkeypatch.setattr(timing, "perf_counter", lambda: ticks[0])

        def sleep(seconds):
            trace.append(("sleep", seconds))
            ticks[0] += seconds

        monkeypatch.setattr("devices.motion.modbus.time.sleep", sleep)
        replies = iter([Reply(), Reply(True), Reply(), Reply()])

        def operation(**kwargs):
            trace.append(kwargs)
            ticks[0] += 0.01
            return next(replies)

        client = ModbusRTUClient()
        client._connected = True
        client._client = SimpleNamespace(socket=None, read_holding_registers=operation)
        with timing.collect(metrics) if enabled else nullcontext():
            with timing.motion_measure({}, "position_snapshot"):
                assert client.read_holding_registers(1, 897) == [545]
            with timing.motion_measure({}, "wait_arrival"):
                assert client.read_holding_registers(2, 897) == [545]
                assert client.read_holding_registers(2, 897) == [545]
        traces.append(trace)
    assert traces[0] == traces[1]
    assert metrics["transaction_count"] == 3
    assert metrics["attempt_count"] == 4
    assert metrics["retry_count"] == metrics["switched_retry_count"] == 1
    assert metrics["same_slave_retry_count"] == 0
    assert metrics["slave_switch_count"] == 1
    assert metrics["transaction_total_ms"] == pytest.approx(90)
    assert metrics["attempt_total_ms"] == pytest.approx(40)
    assert metrics["retry_overhead_ms"] == pytest.approx(60)
    assert sum(g["total_ms"] for g in metrics["groups"].values()) == pytest.approx(90)
    assert len(metrics["groups"]) == 2
    detail = metrics["slow_or_retried"][0]
    assert detail["phase"] == "wait_arrival"
    assert detail["previous_slave"] == 1
    assert detail["since_previous_attempt_ms"] == pytest.approx(0)
    assert len(detail["attempts"]) == 2
    assert timing.current() is None


def test_unexpected_exception_preserved_and_context_reset():
    error = OSError("serial disconnected")

    def fail(**kwargs):
        raise error

    client = ModbusRTUClient()
    client._connected = True
    client._client = SimpleNamespace(read_holding_registers=fail)
    stats = {}
    with pytest.raises(OSError) as caught:
        with timing.collect(stats):
            client.read_holding_registers(1, 897)
    assert caught.value is error
    assert stats["failed_transaction_count"] == 1
    assert stats["attempt_count"] == 1
    assert stats["slow_or_retried"][0]["attempts"][0]["error"].startswith("OSError")
    assert timing.current() is None


def test_bounded_details_keep_full_failure_counts():
    client = ModbusRTUClient()
    stats = {}
    with timing.collect(stats):
        for _ in range(timing.DETAIL_LIMIT + 3):
            assert client._transact("empty", lambda: None, slave=1, address=897) is None
    assert stats["failed_transaction_count"] == timing.DETAIL_LIMIT + 3
    assert len(stats["slow_or_retried"]) == timing.DETAIL_LIMIT
    assert stats["dropped_details"] == 3


def test_lifecycle_measures_without_extra_operations(monkeypatch):
    events = []

    class FakeSerial:
        def __init__(self, **kwargs):
            pass

        def connect(self):
            events.append("connect")
            return True

        def close(self):
            events.append("close")

    monkeypatch.setattr("devices.motion.modbus.ModbusSerialClient", FakeSerial)
    monkeypatch.setattr("devices.motion.modbus.time.sleep", lambda s: events.append(s))
    client = ModbusRTUClient()
    client._timing_last_slave = 2
    stats = {}
    with timing.collect(stats):
        with client:
            assert client._timing_last_slave is None
    assert events == ["connect", 0.05, "close"]
    assert stats["lifecycle_counts"] == {"connect": 1, "disconnect": 1}
    assert set(stats["lifecycle_ms"]) == {"connect", "disconnect"}


def test_nested_context_and_phase_isolation():
    outer, inner = {}, {}
    with timing.collect(outer):
        with timing.motion_measure({}, "axis_ready"):
            with timing.collect(inner):
                assert timing.current() is inner
            assert timing.current() is outer
    assert timing.current() is None
