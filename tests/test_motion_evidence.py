"""Evidence preserves serial commands/retries and survives failed scans."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pymodbus.exceptions import ModbusIOException

from devices.motion import evidence, modbus


class Reply:
    def __init__(self, registers=None, error=False):
        self.registers = registers
        self.error = error

    def isError(self):
        return self.error

    def __str__(self):
        return "No response received"


@pytest.mark.parametrize("failure", ["reply", "exception", "unexpected", "empty"])
def test_transaction_attempts_and_original_error(monkeypatch, failure):
    clock = [100.0]
    monkeypatch.setattr(evidence, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(modbus.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(modbus.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    calls = []
    original = RuntimeError("serial disconnected")

    class Bus:
        socket = None

        def write_register(self, **kwargs):
            calls.append(kwargs)
            clock[0] += 0.3 if len(calls) == 1 else 0.01
            if len(calls) == 1:
                if failure == "exception":
                    raise ModbusIOException("No response received")
                if failure == "unexpected":
                    raise original
                return None if failure == "empty" else Reply(error=True)
            return Reply()

    client = modbus.ModbusRTUClient()
    client._client, client._connected = Bus(), True
    result = {}
    if failure == "unexpected":
        with pytest.raises(RuntimeError) as caught:
            with evidence.record_motion(result):
                client._write_controlword(2, 6)
        assert caught.value is original
        assert result["outcome"] == "raised"
    else:
        with evidence.record_motion(result), evidence.phase("finish_stop_y"):
            assert client._write_controlword(2, 6) is (failure != "empty")
    tx = result["transactions"][0]
    assert tx["slave"] == 2 and tx["address"] == 896 and tx["controlword_hex"] == "0x0006"
    assert tx["attempts"][0]["elapsed_ms"] == pytest.approx(300)
    retried = failure in ("reply", "exception")
    assert result["summary"]["retry_count"] == int(retried)
    assert tx["elapsed_ms"] == pytest.approx(360 if retried else 300)
    assert len(calls) == (2 if retried else 1)
    assert tx["success"] is retried
    assert not evidence.enabled()
    json.dumps(result)


def test_bounded_records_and_thread_isolation(monkeypatch):
    monkeypatch.setattr(evidence, "_MAX_EVENTS", 1)
    monkeypatch.setattr(evidence, "_MAX_TRANSACTIONS", 1)
    result = {}
    with evidence.record_motion(result):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(evidence.enabled).result() is False
        for _ in range(3):
            evidence.event("sample")
            with evidence.transaction("test") as trace:
                evidence.attempt(trace, evidence.perf_counter(), True)
    assert len(result["events"]) == len(result["transactions"]) == 1
    assert result["summary"]["dropped_events"] == 2
    assert result["summary"]["dropped_transactions"] == 2
    assert result["summary"]["transaction_count"] == 3


@pytest.mark.parametrize("diagnostic_read_fails", [False, True])
def test_real_motion_path_preserves_commands_and_records_state(monkeypatch, diagnostic_read_fails):
    from workflow import stage_executor as stage

    buses = []

    class Bus:
        socket = None

        def __init__(self, **kwargs):
            self.calls = []
            self.regs = {}
            self.positions = {1: 0, 2: 0}
            self.states = {1: 0x221, 2: 0x221}
            buses.append(self)

        def connect(self):
            return True

        def close(self):
            pass

        def read_holding_registers(self, *, slave, address, count):
            self.calls.append(("read", slave, address, count))
            if diagnostic_read_fails and evidence.enabled() and evidence._active.get()["phase"] == "after_finish_control":
                raise RuntimeError("diagnostic read failed")
            if address == 897:
                return Reply([self.states[slave]])
            if address == 963:
                return Reply([1])
            if address in (966, 968):
                return Reply([0, self.positions[slave]])
            raise AssertionError(address)

        def write_register(self, *, slave, address, value):
            self.calls.append(("write", slave, address, value))
            self.regs[slave, address] = value
            if address == 896:
                self.states[slave] = {0: 0x40, 6: 0x221, 7: 0x223, 15: 0x227, 31: 0x627}[value]
                if value == 31:
                    self.positions[slave] = self.regs[slave, 1000]
            return Reply()

    monkeypatch.setattr(modbus, "ModbusSerialClient", Bus)
    monkeypatch.setattr(modbus.time, "sleep", lambda seconds: None)
    # Other test modules may have loaded stage against stubs during collection.
    monkeypatch.setattr(stage, "ModbusRTUClient", modbus.ModbusRTUClient)
    from devices.motion.MotorManager import MotorManager
    monkeypatch.setattr(stage, "MotorManager", MotorManager)
    kwargs = dict(port="test", x_target=100, y_target=200, profile_vel=10,
                  profile_acc=20, profile_dec=20, settle_s=0, arrival_tolerance_pulse=0)
    baseline = stage.move_to_absolute(**kwargs)
    record = {}
    with evidence.record_motion(record, point_index=1):
        measured = stage.move_to_absolute(**kwargs)
    assert baseline["after"] == measured["after"]
    assert [c for c in buses[0].calls if c[0] == "write"] == [c for c in buses[1].calls if c[0] == "write"]
    assert len(buses[1].calls) - len(buses[0].calls) == (1 if diagnostic_read_fails else 4)
    assert "diagnostic_snapshot" in measured["timings_ms"]
    events = record["events"]
    arrival = next(e for e in events if e["kind"] == "arrival_tolerance_met")
    assert arrival["standstill_verified"] is False
    before = next(e for e in events if e["kind"] == "axis_snapshot" and e["phase"] == "before_enable")
    assert before["status_bits"]["operation_enabled_bit"] is False
    after = next(e for e in events if e["kind"] == "axis_snapshot" and e["phase"] == "after_settle")
    assert after["statusword"] == 0x221
    stops = [t for t in record["transactions"] if t["phase"].startswith("finish_stop_")]
    assert [t["controlword_hex"] for t in stops] == ["0x0006", "0x0006"]
    ready_reads = [t for t in record["transactions"] if t["phase"] == "axis_ready" and t["address"] == 897]
    assert any(t["attempts"][-1]["status_bits"]["operation_enabled_bit"] for t in ready_reads)
    assert any(e["kind"] == "diagnostic_read_error" for e in events) is diagnostic_read_fails
    assert record["outcome"] == "returned"
