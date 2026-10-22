"""Passive, bounded scan diagnostics: no extra I/O, sleeps or hardware reads."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
import platform
import sys

import pymodbus

from workflow.timing import measure


_active = ContextVar("motion_communication_timing", default=None)
_phase = ContextVar("motion_communication_phase", default="unclassified")
DETAIL_LIMIT = 100


def current():
    return _active.get()


@contextmanager
def collect(result):
    result.update({
        "schema_version": 1, "mode": "passive_linux_baseline_v1",
        "runtime": {"platform": sys.platform, "python": platform.python_version(),
                    "pymodbus": pymodbus.__version__},
        "transaction_count": 0, "attempt_count": 0, "retry_count": 0,
        "failed_transaction_count": 0, "transaction_total_ms": 0.0,
        "attempt_total_ms": 0.0, "retry_overhead_ms": 0.0,
        "slave_switch_count": 0, "switched_retry_count": 0,
        "same_slave_retry_count": 0, "initial_retry_count": 0,
        "slow_threshold_ms": 100.0, "slow_transaction_count": 0,
        "slow_or_retried": [], "dropped_details": 0,
        "groups": {}, "lifecycle_ms": {}, "lifecycle_counts": {},
    })
    token = _active.set(result)
    phase_token = _phase.set("unclassified")
    try:
        yield result
    finally:
        _phase.reset(phase_token)
        _active.reset(token)


@contextmanager
def motion_measure(timings, name):
    """Preserve existing step timing while labeling its communication calls."""
    token = _phase.set(name)
    try:
        with measure(timings, name):
            yield
    finally:
        _phase.reset(token)


@contextmanager
def lifecycle(name):
    stats = current()
    if stats is None:
        yield
        return
    started = perf_counter()
    try:
        yield
    finally:
        stats["lifecycle_ms"][name] = stats["lifecycle_ms"].get(name, 0.0) + (perf_counter() - started) * 1000
        stats["lifecycle_counts"][name] = stats["lifecycle_counts"].get(name, 0) + 1


def record(stats, *, metadata, attempts, elapsed_ms, success, previous_slave, switch_gap_ms):
    slave = metadata.get("slave")
    switched = previous_slave is not None and previous_slave != slave
    retries = max(0, len(attempts) - 1)
    attempt_ms = sum(a["elapsed_ms"] for a in attempts)
    # A successful final attempt is useful work; failed attempts, retry delay
    # and flush overhead before it are retry cost. No retry => no retry cost.
    retry_ms = max(0.0, elapsed_ms - (attempts[-1]["elapsed_ms"] if success else 0.0)) if retries else 0.0
    phase = _phase.get()
    slow = elapsed_ms >= stats["slow_threshold_ms"]
    stats["transaction_count"] += 1
    stats["attempt_count"] += len(attempts)
    stats["retry_count"] += retries
    stats["failed_transaction_count"] += int(not success)
    stats["transaction_total_ms"] += elapsed_ms
    stats["attempt_total_ms"] += attempt_ms
    stats["retry_overhead_ms"] += retry_ms
    stats["slave_switch_count"] += int(switched)
    retry_key = "initial_retry_count" if previous_slave is None else "switched_retry_count" if switched else "same_slave_retry_count"
    stats[retry_key] += retries
    stats["slow_transaction_count"] += int(slow)
    # Register/phase keys are fixed by the motion implementation, not target values.
    key = f"{phase}|{slave}|{metadata.get('function')}|{metadata.get('address')}"
    group = stats["groups"].setdefault(key, {
        "phase": phase, "slave": slave, "function": metadata.get("function"),
        "address": metadata.get("address"), "transaction_count": 0,
        "attempt_count": 0, "retry_count": 0, "failed_transaction_count": 0,
        "total_ms": 0.0, "attempt_total_ms": 0.0, "retry_overhead_ms": 0.0,
        "min_ms": elapsed_ms, "max_ms": 0.0,
    })
    group["transaction_count"] += 1
    group["attempt_count"] += len(attempts)
    group["retry_count"] += retries
    group["failed_transaction_count"] += int(not success)
    group["total_ms"] += elapsed_ms
    group["attempt_total_ms"] += attempt_ms
    group["retry_overhead_ms"] += retry_ms
    group["min_ms"] = min(group["min_ms"], elapsed_ms)
    group["max_ms"] = max(group["max_ms"], elapsed_ms)
    if slow or retries or not success:
        if len(stats["slow_or_retried"]) < DETAIL_LIMIT:
            stats["slow_or_retried"].append({
                **metadata, "phase": phase, "previous_slave": previous_slave,
                "slave_switched": switched, "since_previous_attempt_ms": switch_gap_ms,
                "elapsed_ms": elapsed_ms, "success": success, "attempts": attempts,
            })
        else:
            stats["dropped_details"] += 1
