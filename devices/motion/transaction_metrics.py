"""Per-scan-move communication policy and bounded, in-memory measurements."""
from contextlib import contextmanager
from contextvars import ContextVar
import math


DEFAULT_SLAVE_SWITCH_GAP_S = 0.010
_current = ContextVar("scan_communication", default=None)


def validate_gap(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("motion.slave_switch_gap_s must be a number in [0, 0.1]")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 0.1:
        raise ValueError("motion.slave_switch_gap_s must be a finite number in [0, 0.1]")
    return value


def current():
    return _current.get()


@contextmanager
def scan_communication(result, gap_s=DEFAULT_SLAVE_SWITCH_GAP_S):
    gap_s = validate_gap(gap_s)
    result.update(
        schema_version=1, slave_switch_gap_s=gap_s,
        transaction_count=0, attempt_count=0, retry_count=0,
        failed_transaction_count=0, slave_switch_count=0,
        gap_wait_count=0, gap_wait_ms=0.0, transaction_total_ms=0.0,
        retry_overhead_ms=0.0, slow_transaction_count=0,
        slow_threshold_ms=100.0, slow_or_retried=[], dropped_details=0,
    )
    token = _current.set(result)
    try:
        yield
    finally:
        _current.reset(token)
