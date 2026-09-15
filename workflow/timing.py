"""Monotonic elapsed-time measurements; no logging or I/O in timed steps."""
from __future__ import annotations

from contextlib import contextmanager
from time import perf_counter
from typing import Iterator, MutableMapping


@contextmanager
def measure(timings: MutableMapping[str, float], name: str) -> Iterator[None]:
    """Record milliseconds even when the measured operation raises.

    Repeated names accumulate. Parent measurements include their children and
    must not be added to them when calculating totals.
    """
    started = perf_counter()
    try:
        yield
    finally:
        timings[name] = timings.get(name, 0.0) + (perf_counter() - started) * 1000.0
