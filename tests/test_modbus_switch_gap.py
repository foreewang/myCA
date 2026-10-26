"""Exercise the real read/write wrapper using a simulated clock and transport."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from devices.motion import communication_timing as timing, modbus


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class EmptyReply:
    def isError(self):
        return True

    def __str__(self):
        return "No response received (0 received)"


@pytest.fixture
def bus(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(modbus.time, "perf_counter", lambda: clock.now)
    monkeypatch.setattr(modbus.time, "sleep", clock.sleep)
    outcomes, calls = [], []

    def transact(**kwargs):
        calls.append((clock.now, kwargs))
        clock.now += 0.008
        value = outcomes.pop(0) if outcomes else SimpleNamespace(isError=lambda: False, registers=[545])
        if isinstance(value, BaseException):
            raise value
        return value

    transport = SimpleNamespace(socket=None, read_holding_registers=transact,
                                write_register=transact, connect=lambda: True, close=lambda: None)
    monkeypatch.setattr(modbus, "ModbusSerialClient", lambda **kwargs: transport)

    def client(gap=2):
        result = modbus.ModbusRTUClient(slave_switch_gap_ms=gap)
        result._client, result._connected = transport, True
        return result

    return SimpleNamespace(clock=clock, outcomes=outcomes, calls=calls, client=client)


@pytest.mark.parametrize("value", [True, False, None, "2", -1, float('nan'), float('inf'), 20.1, 10**1000])
def test_invalid_gap_rejected_before_connect(value):
    with pytest.raises(ValueError, match="slave_switch_gap_ms"):
        modbus.ModbusRTUClient(slave_switch_gap_ms=value)


@pytest.mark.parametrize("enabled", [False, True])
def test_guard_applies_to_reads_and_writes_with_or_without_diagnostics(bus, enabled):
    client, stats = bus.client(), {}
    with timing.collect(stats) if enabled else nullcontext():
        assert client.read_holding_registers(1, 897) == [545]
        assert client.write_register(1, 896, 15)
        assert bus.clock.sleeps == []  # first and same slave
        assert client.write_register(2, 896, 15)
        bus.clock.now += 0.00075
        assert client.read_holding_registers(1, 897) == [545]
        bus.clock.now += 0.010
        assert client.read_holding_registers(2, 897) == [545]
    assert bus.clock.sleeps == pytest.approx([0.002, 0.00125])
    if enabled:
        assert stats['switch_guard_count'] == 2
        assert stats['switch_guard_requested_ms'] == pytest.approx(3.25)
        assert stats['switch_guard_wait_ms'] == pytest.approx(3.25)
        assert stats['configured_slave_switch_gaps_ms'] == [2.0]
        assert stats['transaction_total_ms'] == pytest.approx(40)
        assert stats['retry_overhead_ms'] == 0


def test_zero_default_preserves_no_added_wait(bus):
    client = bus.client(gap=0)
    assert modbus.ModbusRTUClient().slave_switch_gap_ms == 0
    client.read_holding_registers(1, 897)
    client.read_holding_registers(2, 897)
    assert bus.clock.sleeps == []


@pytest.mark.parametrize("enabled", [False, True])
def test_retry_delay_and_cache_invalidation_are_independent_of_diagnostics(bus, enabled):
    client, stats = bus.client(), {}
    with timing.collect(stats) if enabled else nullcontext():
        client.read_holding_registers(1, 897)
        bus.outcomes.append(EmptyReply())
        assert client.write_register(2, 999, 123)
    assert bus.clock.sleeps == pytest.approx([0.002, 0.050])
    assert client.configuration_generation[2] == 1
    if enabled:
        assert stats['retry_count'] == 1
        assert stats['switch_guard_wait_ms'] == pytest.approx(2)
        assert stats['transaction_total_ms'] == pytest.approx(74)
        assert stats['retry_overhead_ms'] == pytest.approx(58)
        detail = stats['slow_or_retried'][0]
        assert detail['slave_switch_gap_ms'] == 2
        assert detail['switch_guard_wait_ms'] == pytest.approx(2)
        assert detail['since_previous_attempt_ms'] == pytest.approx(0)
        assert detail['after_guard_gap_ms'] == pytest.approx(2)


def test_exception_preserves_guard_boundary_and_invalidates_cache(bus):
    client = bus.client()
    bus.outcomes.append(OSError('simulated I/O error'))
    with pytest.raises(OSError):
        client.read_holding_registers(1, 897)
    client.read_holding_registers(2, 897)
    assert client.configuration_generation[1] == 1
    assert bus.clock.sleeps == pytest.approx([0.002])


def test_new_connection_resets_guard_state(bus):
    client = bus.client()
    client.read_holding_registers(1, 897)
    client.disconnect()
    assert client._last_transaction_end is None
    assert client.connect()
    bus.clock.sleeps.clear()
    client.read_holding_registers(2, 897)
    assert bus.clock.sleeps == []


def test_guard_reports_sleep_overshoot(bus, monkeypatch):
    client, stats = bus.client(), {}
    def oversleep(seconds):
        bus.clock.now += seconds + 0.0005
    monkeypatch.setattr(modbus.time, 'sleep', oversleep)
    with timing.collect(stats):
        client.read_holding_registers(1, 897)
        client.read_holding_registers(2, 897)
    assert stats['switch_guard_requested_ms'] == pytest.approx(2)
    assert stats['switch_guard_wait_ms'] == pytest.approx(2.5)
