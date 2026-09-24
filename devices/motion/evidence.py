"""Bounded, per-move evidence. Never changes motion decisions or writes logs."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from time import perf_counter


_active = ContextVar("motion_evidence", default=None)
_MAX_TRANSACTIONS = 5000
_MAX_EVENTS = 4096


def status_bits(value):
    """Raw CiA 402 bit indications, NOT a vendor-validated state diagnosis."""
    if value is None:
        return None
    return {
        "hex": f"0x{value:04X}",
        "operation_enabled_bit": bool(value & 0x0004),
        "fault_bit": bool(value & 0x0008),
        "quick_stop_bit_5": bool(value & 0x0020),
        "switch_on_disabled_bit": bool(value & 0x0040),
        "target_reached_bit": bool(value & 0x0400),
        "setpoint_ack_bit": bool(value & 0x2000),
    }


@contextmanager
def record_motion(result, **identity):
    started = perf_counter()
    result.update(
        schema_version=1, identity=identity, started_monotonic_s=started,
        started_utc=datetime.now(timezone.utc).isoformat(),
        events=[], transactions=[],
        summary={"transaction_count": 0, "attempt_count": 0,
                 "retry_count": 0, "failed_transaction_count": 0,
                 "slow_transaction_count": 0, "transaction_total_ms": 0.0,
                 "dropped_transactions": 0, "dropped_events": 0},
        slow_threshold_ms=100.0,
        interpretation="Status bits are indications only; position tolerance does not prove standstill. "
                       "Snapshots are sequential reads, not simultaneous. Post-finish diagnostic reads add latency.",
    )
    token = _active.set({"result": result, "started": started, "phase": "move_start"})
    try:
        yield
    except BaseException as exc:
        result["outcome"] = "raised"
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    else:
        result["outcome"] = "returned"
    finally:
        result["elapsed_ms"] = (perf_counter() - started) * 1000
        _active.reset(token)


def enabled():
    return _active.get() is not None


@contextmanager
def phase(name):
    state = _active.get()
    if state is None:
        yield
        return
    previous = state["phase"]
    state["phase"] = name
    try:
        yield
    finally:
        state["phase"] = previous


def event(kind, **values):
    state = _active.get()
    if state is None:
        return
    result = state["result"]
    item = {"kind": kind, "phase": state["phase"],
            "t_ms": (perf_counter() - state["started"]) * 1000, **values}
    if len(result["events"]) < _MAX_EVENTS:
        result["events"].append(item)
    else:
        result["summary"]["dropped_events"] += 1


@contextmanager
def transaction(description, **metadata):
    state = _active.get()
    if state is None:
        yield None
        return
    started = perf_counter()
    item = {"description": description, "phase": state["phase"], **metadata,
            "t_ms": (started - state["started"]) * 1000, "attempts": []}
    if metadata.get("function") == "write_register" and metadata.get("address") == 896:
        item["controlword_hex"] = f"0x{metadata['value']:04X}"
    try:
        yield item
    finally:
        item["elapsed_ms"] = (perf_counter() - started) * 1000
        item["success"] = bool(item["attempts"] and item["attempts"][-1]["success"])
        result = state["result"]
        summary = result["summary"]
        summary["transaction_count"] += 1
        summary["attempt_count"] += len(item["attempts"])
        summary["retry_count"] += max(0, len(item["attempts"]) - 1)
        summary["failed_transaction_count"] += int(not item["success"])
        summary["slow_transaction_count"] += int(item["elapsed_ms"] >= result["slow_threshold_ms"])
        summary["transaction_total_ms"] += item["elapsed_ms"]
        if len(result["transactions"]) < _MAX_TRANSACTIONS:
            result["transactions"].append(item)
        else:
            summary["dropped_transactions"] += 1


def attempt(trace, started, success, *, error=None, registers=None):
    if trace is None:
        return
    item = {"attempt": len(trace["attempts"]) + 1,
            "started_monotonic_s": started,
            "elapsed_ms": (perf_counter() - started) * 1000,
            "success": success}
    if error is not None:
        item["error"] = str(error)
    if registers is not None:
        item["registers"] = list(registers)
        if trace.get("address") == 897 and item["registers"]:
            item["status_bits"] = status_bits(item["registers"][0])
    trace["attempts"].append(item)
