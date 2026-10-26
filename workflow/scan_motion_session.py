"""Exclusive, scan-local motion resource; never shared with autofocus or another well."""
from contextlib import contextmanager

from devices.motion.communication_timing import lifecycle
from devices.motion.modbus import validate_slave_switch_gap_ms


def optimization_options(motion):
    raw = motion.get("stop_shoot", {})
    if not isinstance(raw, dict):
        raise ValueError("motion.stop_shoot must be an object")
    defaults = {"enabled": True, "reuse_connection": True, "reuse_profile": True, "hold_enabled": False}
    unknown = set(raw) - set(defaults)
    if unknown:
        raise ValueError(f"unknown motion.stop_shoot options: {sorted(unknown)}")
    for name, value in raw.items():
        if not isinstance(value, bool):
            raise ValueError(f"motion.stop_shoot.{name} must be boolean")
    defaults.update(raw)
    if defaults["hold_enabled"] and not (defaults["enabled"] and defaults["reuse_connection"]):
        raise ValueError("hold_enabled requires enabled and reuse_connection")
    return defaults


class ScanMotionSession:
    def __init__(self, options):
        self.options = options
        self.client = None
        self.identity = None
        self.slave_switch_gap_ms = None
        self.profiles = {}
        self.needs_stop = False

    @contextmanager
    def connection(self, factory, *, port, baudrate, x_slave, y_slave, release=False,
                   slave_switch_gap_ms=0.0):
        slave_switch_gap_ms = validate_slave_switch_gap_ms(slave_switch_gap_ms)
        identity = (port, baudrate, x_slave, y_slave)
        if self.client is not None and self.identity != identity:
            raise RuntimeError("scan motion connection identity changed")
        if self.client is not None and self.slave_switch_gap_ms != slave_switch_gap_ms:
            raise RuntimeError("scan motion slave switch gap changed during an active connection")
        try:
            if self.client is None:
                self.identity = identity
                self.slave_switch_gap_ms = slave_switch_gap_ms
                gap_options = {"slave_switch_gap_ms": slave_switch_gap_ms} if slave_switch_gap_ms else {}
                self.client = factory(port=port, baudrate=baudrate, **gap_options)
                with lifecycle("connect"):
                    if not self.client.connect():
                        raise RuntimeError(f"failed to connect scan motion port {port}")
            if not self.client.is_connected():
                raise RuntimeError("scan motion connection lost; cached parameters invalid")
            yield self.client
        except BaseException:
            self.profiles.clear()
            try:
                self.close()
            except Exception:
                # Keep the original failure; caller reports/records cleanup errors.
                import logging
                logging.getLogger(__name__).exception("scan motion failure cleanup failed")
            raise
        else:
            if release or not self.options["reuse_connection"]:
                self.close()

    def close(self):
        client, identity = self.client, self.identity
        self.profiles.clear()
        if client is None:
            return
        errors = []
        try:
            if self.needs_stop:
                for slave in identity[2:4]:
                    try:
                        if not client.quick_stop(slave):
                            errors.append(f"axis {slave} stop returned false")
                    except Exception as exc:
                        errors.append(f"axis {slave} stop failed: {exc}")
            with lifecycle("disconnect"):
                client.disconnect()
        finally:
            self.client = None
            self.identity = None
            self.slave_switch_gap_ms = None
            self.needs_stop = False
        if errors:
            raise RuntimeError("; ".join(errors))
