from contextlib import nullcontext

import pytest

from devices.motion import communication_timing as timing
from devices.motion.modbus import ModbusRTUClient


class Reply:
    def __init__(self, error=False):
        self.error = error

    def isError(self):
        return self.error

    def __str__(self):
        return "IllegalAddress" if self.error else "ok"


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "case", ["success", "empty", "error", "exception", "interrupt"]
)
def test_cache_invalidation_independent_of_diagnostics(enabled, case):
    client = ModbusRTUClient(port="unused")
    client.configuration_generation = {1: 7, 2: 11}
    stats = {}

    def operation():
        if case == "exception":
            raise RuntimeError("test failure")
        if case == "interrupt":
            raise KeyboardInterrupt()
        if case == "empty":
            return None
        return Reply(error=case == "error")

    with timing.collect(stats) if enabled else nullcontext():
        if case in ("exception", "interrupt"):
            error = RuntimeError if case == "exception" else KeyboardInterrupt
            with pytest.raises(error):
                client._transact("test", operation, slave=1)
        else:
            result = client._transact("test", operation, slave=1)
            if case == "success":
                assert result is not None
            else:
                assert result is None

    assert client.configuration_generation[1] == (
        7 if case == "success" else 8
    )
    assert client.configuration_generation[2] == 11
    if not enabled:
        assert stats == {}
