"""Keep persistent safety-state tests isolated from the deployment data tree."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from workflow import hardware_guard


_SAFETY_STATE_DIR = tempfile.TemporaryDirectory(prefix="colony-safety-tests-")
_SAFETY_STATE_PATH = Path(_SAFETY_STATE_DIR.name) / "hardware_safety_latch.json"
_ORIGINAL_SAFETY_STATE_PATH = hardware_guard.DEFAULT_HARDWARE_SAFETY_LATCH_PATH
hardware_guard.DEFAULT_HARDWARE_SAFETY_LATCH_PATH = _SAFETY_STATE_PATH


@pytest.fixture(autouse=True)
def _isolate_hardware_safety_state_between_tests():
    """Never let a test generation marker poison the next test."""

    hardware_guard._reset_hardware_guard_for_tests(
        clear_persisted_latch=True,
        forget_persistence_path=True,
    )
    yield
    hardware_guard._reset_hardware_guard_for_tests(
        clear_persisted_latch=True,
        forget_persistence_path=True,
    )


def pytest_sessionfinish(session, exitstatus) -> None:
    # The directory is test-owned and exact; never touch the production latch.
    del session, exitstatus
    hardware_guard._reset_hardware_guard_for_tests(
        clear_persisted_latch=True,
        forget_persistence_path=True,
    )
    hardware_guard.DEFAULT_HARDWARE_SAFETY_LATCH_PATH = _ORIGINAL_SAFETY_STATE_PATH
    _SAFETY_STATE_DIR.cleanup()
