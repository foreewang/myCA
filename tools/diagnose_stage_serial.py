"""Stationary, read-only XY Modbus diagnosis. Linux, Python 3.10, PyModbus 3.6.9.

Run outside the service after stopping it. No register writes, enables, stops,
homing, camera operations or application configuration edits are performed.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import subprocess
import sys
import time


ALLOWED_READS = {(968, 2), (897, 1)}


def now():
    return datetime.now(timezone.utc).isoformat()


def require_idle(root, service, port):
    """Checks only; never stops a service or kills a process."""
    state = subprocess.run(
        ["systemctl", "show", service,
         "--property=LoadState,ActiveState,WorkingDirectory,MainPID,ControlPID", "--no-pager"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    fields = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
    if (fields.get("LoadState") != "loaded"
            or fields.get("ActiveState") not in {"inactive", "failed"}
            or fields.get("MainPID") != "0" or fields.get("ControlPID") != "0"):
        raise ValueError("Service must exist and be stopped, with MainPID=ControlPID=0")
    if not fields.get("WorkingDirectory") or Path(fields["WorkingDirectory"]).resolve() != root:
        raise ValueError("Service WorkingDirectory does not match --root")
    if not port.exists() or not port.is_char_device():
        raise ValueError("Port must be an existing tty character device")
    owners = subprocess.run(["fuser", "-v", str(port)], capture_output=True, text=True, timeout=10)
    if owners.returncode == 0 or owners.stdout.strip():
        raise ValueError("Serial port is in use; stop its owner first: " + owners.stdout.strip())
    if owners.returncode != 1 or owners.stderr.strip():
        raise ValueError("Cannot verify serial port ownership: " + owners.stderr.strip())
    return fields


class TransportProbe:
    def __init__(self):
        self.counts = Counter()
        self.times_ms = Counter()
        self.events = deque(maxlen=32)

    def event(self, kind, elapsed_ms, **fields):
        self.counts[kind] += 1
        self.times_ms[kind] += elapsed_ms
        self.events.append({"kind": kind, "at_monotonic_s": time.monotonic(),
                            "elapsed_ms": elapsed_ms, **fields})

    def summary(self):
        return {"counts": dict(self.counts), "timings_ms": dict(self.times_ms)}


def observed_serial_class(base, probe, slaves):
    """Observe the library, including implicit reopens; preserve its behavior."""
    def is_open(client):
        socket = getattr(client, "socket", None)
        return socket is not None and bool(getattr(socket, "is_open", False))

    class ReadOnlySerial(base):
        def execute(self, request=None):
            if (getattr(request, "function_code", None) != 3
                    or getattr(request, "slave_id", None) not in slaves
                    or (getattr(request, "address", None), getattr(request, "count", None)) not in ALLOWED_READS):
                raise ValueError("Diagnostic blocked a non-allowlisted Modbus request")
            return super().execute(request)

        def connect(self):
            if is_open(self):
                return super().connect()
            start = time.monotonic()
            try:
                return super().connect()
            finally:
                probe.event("serial_open" if is_open(self) else "serial_open_failed",
                            (time.monotonic() - start) * 1000)

        def close(self):
            was_open = is_open(self)
            start = time.monotonic()
            try:
                return super().close()
            finally:
                if was_open:
                    probe.event("serial_close", (time.monotonic() - start) * 1000)

        def send(self, request):
            start = time.monotonic()
            sent = 0
            try:
                sent = super().send(request)
                return sent
            finally:
                probe.event("send", (time.monotonic() - start) * 1000,
                            bytes=sent if isinstance(sent, int) else None, hex=bytes(request).hex())

        def recv(self, size):
            start = time.monotonic()
            data = b""
            try:
                data = super().recv(size)
                return data
            finally:
                probe.event("recv", (time.monotonic() - start) * 1000,
                            requested_bytes=size, bytes=len(data), hex=bytes(data).hex())

    return ReadOnlySerial


@contextmanager
def observe_transport(modbus, probe, slaves):
    original = modbus.ModbusSerialClient
    modbus.ModbusSerialClient = observed_serial_class(original, probe, slaves)
    try:
        yield
    finally:
        modbus.ModbusSerialClient = original


def cases_for_round(round_index, x, y):
    x_pos, y_pos = (x, 968, 2), (y, 968, 2)
    gaps = [2, 5, 10] if round_index % 2 else [10, 5, 2]
    singles = [("x_only", [x_pos], 0), ("y_only", [y_pos], 0)]
    if round_index % 2 == 0:
        singles.reverse()
    return [*singles, ("snapshot_xy_0ms", [x_pos, (x, 897, 1), y_pos, (y, 897, 1)], 0),
            ("alternate_0ms_before", [x_pos, y_pos], 0),
            *[(f"alternate_{gap}ms", [x_pos, y_pos], gap) for gap in gaps],
            ("alternate_0ms_after", [x_pos, y_pos], 0)]


def wait_for_gap(previous_slave, slave, previous_end, gap_ms, clock=time.monotonic, sleep=time.sleep):
    waited = 0.0
    if previous_end is not None and slave != previous_slave and gap_ms > 0:
        remaining = gap_ms / 1000 - (clock() - previous_end)
        if remaining > 0:
            start = clock()
            sleep(remaining)
            waited = (clock() - start) * 1000
    return waited


def signed_position(registers):
    value = (registers[0] << 16) | registers[1]
    return value - (1 << 32) if value & (1 << 31) else value


def run_case(modbus, timing, *, pattern, gap_ms, reads, seconds, port, baudrate, slaves):
    probe = TransportProbe()
    result = {"status": "running", "started_utc": now(), "minimum_switch_gap_ms": gap_ms,
              "pattern": pattern, "requested_reads": reads, "case_time_limit_s": seconds,
              "samples": [], "directions": {}, "positions": {}, "transport_failures": []}
    stats, phases = {}, {}
    previous_slave = previous_end = None
    failures = 0
    result["guard_wait_ms"] = 0.0
    client = None
    case_start = time.monotonic()
    loop_start = None
    pending_read = None
    with observe_transport(modbus, probe, slaves), timing.collect(stats):
        try:
            client = modbus.ModbusRTUClient(port=str(port), baudrate=baudrate)
            if not client.connect():
                raise RuntimeError("Cannot open serial port")
            loop_start = time.monotonic()
            result["status"] = "complete"
            for index in range(reads):
                if time.monotonic() - loop_start >= seconds:
                    result["status"] = "time_limit"
                    break
                slave, address, count = pattern[index % len(pattern)]
                waited = wait_for_gap(previous_slave, slave, previous_end, gap_ms)
                result["guard_wait_ms"] += waited
                started = time.monotonic()
                switched = previous_slave is not None and slave != previous_slave
                direction = "initial" if previous_slave is None else f"{previous_slave}->{slave}"
                bucket = result["directions"].setdefault(direction, {
                    "transactions": 0, "retries": 0, "retried_transactions": 0, "failed_transactions": 0})
                retries_before = stats["retry_count"]
                opens_before = probe.counts["serial_open"]
                pending_read = {"index": index + 1, "slave": slave, "address": address,
                                "count": count, "previous_slave": previous_slave, "switched": switched,
                                "started_monotonic_s": started}
                with timing.motion_measure(phases, "stationary_read"):
                    registers = client.read_holding_registers(slave, address, count)
                ended = time.monotonic()
                ok = (isinstance(registers, list) and len(registers) == count
                      and all(isinstance(v, int) and 0 <= v <= 65535 for v in registers))
                retries = stats["retry_count"] - retries_before
                sample = {"index": index + 1, "slave": slave, "address": address, "count": count,
                          "previous_slave": previous_slave, "switched": switched,
                          "software_gap_ms": None if previous_end is None else (started - previous_end) * 1000,
                          "guard_wait_ms": waited, "elapsed_ms": (ended - started) * 1000,
                          "retry_count": retries, "serial_reopens": probe.counts["serial_open"] - opens_before,
                          "success": ok, "registers": registers}
                result["samples"].append(sample)
                pending_read = None
                bucket["transactions"] += 1
                bucket["retries"] += retries
                bucket["retried_transactions"] += int(retries > 0)
                bucket["failed_transactions"] += int(not ok)
                if retries or not ok:
                    if len(result["transport_failures"]) < 50:
                        result["transport_failures"].append({"sample_index": index + 1, "events": list(probe.events)})
                    else:
                        result["transport_failure_details_dropped"] = result.get("transport_failure_details_dropped", 0) + 1
                if ok and address == 968:
                    pos = signed_position(registers)
                    values = result["positions"].setdefault(str(slave), {"first": pos, "last": pos, "min": pos, "max": pos})
                    values.update(last=pos, min=min(values["min"], pos), max=max(values["max"], pos))
                previous_slave, previous_end = slave, ended
                failures = 0 if ok else failures + 1
                if failures >= 3:
                    result["status"] = "three_consecutive_failures"
                    break
        except KeyboardInterrupt:
            result["status"] = "interrupted"
            result["last_transport_events"] = list(probe.events)
        except Exception as exc:
            result["status"] = "error"
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["last_transport_events"] = list(probe.events)
        finally:
            if pending_read is not None:
                result["unfinished_read"] = {
                    **pending_read, "elapsed_ms": (time.monotonic() - pending_read["started_monotonic_s"]) * 1000,
                    "retry_count": stats["retry_count"] - retries_before,
                    "serial_reopens": probe.counts["serial_open"] - opens_before}
            result["read_loop_elapsed_ms"] = None if loop_start is None else (time.monotonic() - loop_start) * 1000
            if client is not None:
                try:
                    client.disconnect()
                except Exception as exc:
                    result["status"] = "error"
                    result["close_error"] = str(exc)
    result.update(finished_utc=now(), case_elapsed_ms=(time.monotonic() - case_start) * 1000,
                  communication=stats, transport=probe.summary(), completed_reads=len(result["samples"]))
    for position in result["positions"].values():
        position["span_pulse"] = position["max"] - position["min"]
    switches = sum(s["switched"] for s in result["samples"])
    result["switches"] = switches
    switched_retries = sum(s["retry_count"] for s in result["samples"] if s["switched"])
    result["retries_per_1000_switches"] = switched_retries * 1000 / switches if switches else None
    retried = sum(s["retry_count"] > 0 for s in result["samples"])
    switched_retried = sum(s["switched"] and s["retry_count"] > 0 for s in result["samples"])
    result["retried_transactions"] = retried
    result["retried_transactions_per_1000_reads"] = retried * 1000 / len(result["samples"]) if result["samples"] else None
    result["retried_transactions_per_1000_switches"] = switched_retried * 1000 / switches if switches else None
    completed_retries = sum(s["retry_count"] for s in result["samples"])
    result["retry_attempts_per_1000_reads"] = completed_retries * 1000 / len(result["samples"]) if result["samples"] else None
    return result


def save_report(path, report):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--service", default="colony-system-api")
    parser.add_argument("--port", type=Path, default=Path("/dev/ttyUSB0"))
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--x-slave", type=int, default=1)
    parser.add_argument("--y-slave", type=int, default=2)
    parser.add_argument("--reads", type=int, default=200, help="Transactions per case, not retry attempts")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--case-seconds", type=float, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux":
        raise ValueError("Hardware diagnosis must run on Linux")
    if os.geteuid() != 0:
        raise ValueError("Run as root so fuser can check every process")
    if (not 4 <= args.reads <= 5000 or not 1 <= args.rounds <= 10
            or not 1 <= args.case_seconds <= 120 or args.baudrate <= 0
            or not 1 <= args.x_slave <= 247 or not 1 <= args.y_slave <= 247
            or args.x_slave == args.y_slave or not args.service or args.service.startswith("-")):
        raise ValueError("Invalid parameters; see --help")
    root = args.root.resolve(strict=True)
    port = args.port.resolve(strict=True)
    if not (root / "devices/motion/modbus.py").is_file():
        raise ValueError("--root must be the deployed project")
    require_idle(root, args.service, port)
    sys.path.insert(0, str(root))
    import pymodbus
    from devices.motion import communication_timing as timing, modbus
    if pymodbus.__version__ != "3.6.9":
        raise ValueError("This probe was validated for PyModbus 3.6.9; do not upgrade dependencies to run it")
    logging.getLogger("devices.motion.modbus").setLevel(logging.CRITICAL)
    logging.getLogger("pymodbus").setLevel(logging.CRITICAL)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write("{}\n")  # refuse overwriting an existing result before opening the port
    report = {"schema_version": 1, "kind": "stationary_read_only_serial_diagnostic",
              "status": "running", "started_utc": now(), "kernel": platform.release(),
              "python": platform.python_version(), "pymodbus": pymodbus.__version__,
              "communication_settings": {"timeout_s": modbus._RTU_TIMEOUT_S,
                                         "max_attempts": modbus._RTU_COMM_ATTEMPTS,
                                         "retry_delay_s": modbus._RTU_RETRY_DELAY_S,
                                         "bytesize": 8, "parity": "N", "stopbits": 1},
              "parameters": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
              "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                for name in ("devices/motion/modbus.py", "devices/motion/communication_timing.py")},
              "notes": ["Only FC03 reads of position 968/2 and statusword 897/1 are allowed.",
                        "Every case starts a new connection; library reopen/close events are included in transport counts.",
                        "Serial open/close counts describe handles, not USB reconnects; recv counts describe calls, not response frames.",
                        "Gap timestamps are software API timestamps, not physical RS485 frame timing.",
                        "case_elapsed_ms includes guards, connect/close, transactions and probe overhead; do not sum overlapping timings.",
                        "Cases are bounded by read count and soft time limit; an in-flight transaction may exceed the limit.",
                        "No warm-up reads are excluded. retry_count counts extra attempts, not transactions with retries.",
                        "Rates use completed read calls; on interruption inspect the unfinished transaction in communication details separately.",
                        "No-retry idle results do not rule out problems under motor load. The probe itself changes timing."],
              "cases": []}
    exit_code = 0
    try:
        save_report(output, report)
        for round_index in range(1, args.rounds + 1):
            for name, pattern, gap_ms in cases_for_round(round_index, args.x_slave, args.y_slave):
                require_idle(root, args.service, port)
                print(f"Round {round_index}/{args.rounds}: {name}", flush=True)
                case = run_case(modbus, timing, pattern=pattern, gap_ms=gap_ms, reads=args.reads,
                                seconds=args.case_seconds, port=port, baudrate=args.baudrate,
                                slaves=(args.x_slave, args.y_slave))
                case.update(name=name, round=round_index)
                report["cases"].append(case)
                save_report(output, report)
                c = case["communication"]
                print(f"  {case['status']} reads={case['completed_reads']} retries={c['retry_count']} "
                      f"failed={c['failed_transaction_count']} elapsed={case['case_elapsed_ms']/1000:.2f}s "
                      f"serial_opens={case['transport']['counts'].get('serial_open', 0)}", flush=True)
                if case["status"] not in {"complete", "time_limit"}:
                    report["status"] = case["status"]
                    exit_code = 130 if case["status"] == "interrupted" else 1
                    return exit_code
        report["status"] = "completed_with_limits" if any(c["status"] == "time_limit" for c in report["cases"]) else "complete"
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        exit_code = 130
    except Exception as exc:
        report["status"] = "error"
        report["error"] = f"{type(exc).__name__}: {exc}"
        exit_code = 1
    finally:
        report["finished_utc"] = now()
        save_report(output, report)
        print(f"Result: {output}\nService remains stopped; restart it after reviewing the result.", flush=True)
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
