from __future__ import annotations

from pymodbus.exceptions import ModbusIOException

from devices.motion.modbus import ModbusRTUClient


class _OkWrite:
    def isError(self) -> bool:
        return False


class _OkRead:
    def __init__(self, registers: list[int]):
        self.registers = registers

    def isError(self) -> bool:
        return False


class _EmptyError:
    def isError(self) -> bool:
        return True

    def __str__(self) -> str:
        return "No response received, expected at least 4 bytes (0 received)"


class _IllegalAddress:
    def isError(self) -> bool:
        return True

    def __str__(self) -> str:
        return "ExceptionResponse(131, 2, IllegalAddress)"


def _connected_client() -> ModbusRTUClient:
    client = ModbusRTUClient(port="/dev/ttyUSB0")
    client._connected = True
    return client


def test_connect_uses_usb_rs485_timing(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeSerial:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def connect(self) -> bool:
            return True

        def close(self) -> None:
            return None

    monkeypatch.setattr("devices.motion.modbus.ModbusSerialClient", FakeSerial)
    monkeypatch.setattr("devices.motion.modbus.time.sleep", lambda _seconds: None)

    client = ModbusRTUClient(port="/dev/ttyUSB0", timeout=0.3)
    assert client.connect() is True
    assert captured["port"] == "/dev/ttyUSB0"
    assert captured["timeout"] == 0.3
    assert captured["retries"] == 0
    assert captured["retry_on_empty"] is True
    assert captured["strict"] is False


def test_write_register_retries_empty_response(monkeypatch) -> None:
    monkeypatch.setattr("devices.motion.modbus.time.sleep", lambda _seconds: None)
    client = _connected_client()
    calls = {"n": 0}

    class FakeBus:
        socket = None

        def write_register(self, **_kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                return _EmptyError()
            return _OkWrite()

    client._client = FakeBus()
    assert client.write_register(2, 896, 0x0F) is True
    assert calls["n"] == 3


def test_write_register_retries_modbus_io_exception(monkeypatch) -> None:
    monkeypatch.setattr("devices.motion.modbus.time.sleep", lambda _seconds: None)
    client = _connected_client()
    calls = {"n": 0}

    class FakeBus:
        socket = None

        def write_register(self, **_kwargs):
            calls["n"] += 1
            if calls["n"] < 2:
                raise ModbusIOException("No response received, expected at least 4 bytes (0 received)")
            return _OkWrite()

    client._client = FakeBus()
    assert client._write_controlword(2, 0x0F) is True
    assert calls["n"] == 2


def test_write_register_does_not_retry_slave_exception(monkeypatch) -> None:
    monkeypatch.setattr("devices.motion.modbus.time.sleep", lambda _seconds: None)
    client = _connected_client()
    calls = {"n": 0}

    class FakeBus:
        socket = None

        def write_register(self, **_kwargs):
            calls["n"] += 1
            return _IllegalAddress()

    client._client = FakeBus()
    assert client.write_register(2, 896, 0x0F) is False
    assert calls["n"] == 1


def test_read_holding_registers_retries_then_returns_values(monkeypatch) -> None:
    monkeypatch.setattr("devices.motion.modbus.time.sleep", lambda _seconds: None)
    client = _connected_client()
    calls = {"n": 0}

    class FakeBus:
        socket = None

        def read_holding_registers(self, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _EmptyError()
            return _OkRead([1])

    client._client = FakeBus()
    assert client.read_holding_registers(2, 963, 1) == [1]
    assert calls["n"] == 2


def test_write_register_gives_up_after_persistent_empty_response(monkeypatch) -> None:
    monkeypatch.setattr("devices.motion.modbus.time.sleep", lambda _seconds: None)
    client = _connected_client()
    calls = {"n": 0}

    class FakeBus:
        socket = None

        def write_register(self, **_kwargs):
            calls["n"] += 1
            return _EmptyError()

    client._client = FakeBus()
    assert client.write_register(2, 896, 0x0F) is False
    assert calls["n"] == 3


def test_enable_motor_skips_when_already_enabled(monkeypatch) -> None:
    monkeypatch.setattr("devices.motion.modbus.time.sleep", lambda _seconds: None)
    client = _connected_client()
    writes: list[dict[str, object]] = []

    class FakeBus:
        socket = None

        def read_holding_registers(self, **_kwargs):
            return _OkRead([0x0004])

        def write_register(self, **kwargs):
            writes.append(dict(kwargs))
            return _OkWrite()

    client._client = FakeBus()
    assert client.enable_motor(2) is True
    assert writes == []
