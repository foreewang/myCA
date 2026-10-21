"""Deterministic bus timing tests: no serial hardware or real sleeps."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from pymodbus.exceptions import ModbusIOException

from devices.motion import modbus
from devices.motion.transaction_metrics import scan_communication, current
from workflow.platform_defaults import apply_motion_profile_defaults


class Reply:
    registers = [1]

    def __init__(self, error=False):
        self.error = error

    def isError(self):
        return self.error

    def __str__(self):
        return "No response received"


@pytest.fixture
def bus(monkeypatch):
    class ClockBus:
        socket = None

        def __init__(self):
            self.now = 10.0
            self.sleeps = []
            self.calls = []
            self.responses = []

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds

        def call(self, function, **kwargs):
            self.calls.append((function, self.now, kwargs))
            self.now += 0.002
            response = self.responses.pop(0) if self.responses else Reply()
            if isinstance(response, Exception):
                raise response
            return response

        def read_holding_registers(self, **kwargs):
            return self.call("read", **kwargs)

        def write_register(self, **kwargs):
            return self.call("write", **kwargs)

        def close(self):
            pass

    obj = ClockBus()
    monkeypatch.setattr(modbus.time, "perf_counter", lambda: obj.now)
    monkeypatch.setattr(modbus.time, "sleep", obj.sleep)
    client = modbus.ModbusRTUClient()
    client._client, client._connected = obj, True
    return client, obj


def test_only_top_up_cross_slave_gap_for_reads_and_writes(bus):
    client, clock = bus
    stats = {}
    with scan_communication(stats):
        client._read_statusword(1)  # First request: no wait.
        client._write_controlword(1, 15)  # Same slave: no wait.
        clock.now += 0.003
        client._read_statusword(2)  # Only top up missing 7 ms.
        client._write_controlword(1, 15)  # Full 10 ms.
        clock.now += 0.020
        client._read_statusword(2)  # Existing wait is sufficient.
    assert clock.sleeps == pytest.approx([0.007, 0.010])
    assert stats["slave_switch_count"] == 3
    assert stats["gap_wait_count"] == 2
    assert stats["gap_wait_ms"] == pytest.approx(17)
    assert stats["transaction_total_ms"] == pytest.approx(27)
    assert stats["transaction_count"] == stats["attempt_count"] == 5
    assert [c[2]["value"] for c in clock.calls if c[0] == "write"] == [15, 15]


@pytest.mark.parametrize("gap", [0, 0.005, 0.010])
def test_configurable_gap_and_baseline(bus, gap):
    client, clock = bus
    stats = {}
    with scan_communication(stats, gap):
        client._read_statusword(1)
        client._read_statusword(2)
    assert clock.sleeps == pytest.approx([gap] if gap else [])
    assert stats["gap_wait_ms"] == pytest.approx(gap * 1000)


@pytest.mark.parametrize("gap, expected_retries", [(0, 1), (0.01, 0)])
def test_gap_prevents_simulated_cross_slave_timeout(bus, gap, expected_retries):
    client, clock = bus
    original = clock.call
    last = {"slave": None, "end": None}

    def sensitive_bus(function, **kwargs):
        switched = last["slave"] is not None and last["slave"] != kwargs["slave"]
        too_soon = switched and clock.now - last["end"] < 0.01 - 1e-9
        if too_soon:
            clock.responses = [Reply(True)]
            clock.now += 0.7  # Model the observed blocking SDK timeout.
        reply = original(function, **kwargs)
        last.update(slave=kwargs["slave"], end=clock.now)
        return reply

    clock.call = sensitive_bus
    stats = {}
    with scan_communication(stats, gap):
        assert client._read_statusword(1) == 1
        assert client._read_statusword(2) == 1
    assert stats["retry_count"] == expected_retries
    assert stats["failed_transaction_count"] == 0
    assert stats["transaction_total_ms"] == pytest.approx(756 if gap == 0 else 14)


@pytest.mark.parametrize("response", [Reply(True), ModbusIOException("No response received")])
def test_retry_policy_is_preserved_and_measured(bus, response):
    client, clock = bus
    clock.responses = [response, Reply()]
    stats = {}
    with scan_communication(stats):
        assert client._write_controlword(2, 6)
    assert clock.sleeps == [0.05]
    assert stats["retry_count"] == 1 and stats["attempt_count"] == 2
    assert stats["retry_overhead_ms"] == pytest.approx(52)
    detail = stats["slow_or_retried"][0]
    assert detail["value"] == 6 and detail["success"] is True
    assert detail["attempts"][0]["error"]


@pytest.mark.parametrize("failure", [RuntimeError("broken"), Reply(True), None])
def test_failed_attempt_updates_gap_state_and_preserves_error(bus, failure):
    client, clock = bus
    clock.responses = [failure] * (3 if isinstance(failure, Reply) else 1)
    stats = {}
    with scan_communication(stats):
        if isinstance(failure, Exception):
            with pytest.raises(RuntimeError) as caught:
                client._read_statusword(1)
            assert caught.value is failure
        else:
            assert client._read_statusword(1) is None
        before_next = clock.now
        assert client._read_statusword(2) == 1
    assert clock.calls[-1][1] - before_next == pytest.approx(0.01)
    assert stats["failed_transaction_count"] == 1


def test_reconnect_and_outside_scan_do_not_reuse_policy(bus):
    client, clock = bus
    with scan_communication({}):
        client._read_statusword(1)
        client.disconnect()
        client._connected = True
        client._read_statusword(2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(current).result() is None
    client._read_statusword(1)
    assert clock.sleeps == []


def test_exception_restores_context_and_details_are_bounded(bus):
    client, clock = bus
    stats = {}
    with pytest.raises(ValueError):
        with scan_communication(stats):
            for _ in range(205):
                clock.responses = [None]
                client._read_statusword(1)
            raise ValueError("stop")
    assert current() is None
    assert stats["failed_transaction_count"] == 205
    assert len(stats["slow_or_retried"]) == 200
    assert stats["dropped_details"] == 5


@pytest.mark.parametrize("value", [-1, 0.101, True, None, "0.01", float("nan"), float("inf")])
def test_reject_invalid_gap_before_runtime(value):
    with pytest.raises(ValueError, match="slave_switch_gap_s"):
        apply_motion_profile_defaults({"slave_switch_gap_s": value})
