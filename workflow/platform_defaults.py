"""Linux industrial-PC defaults for serial ports, install root, and MVS paths."""
from __future__ import annotations

from devices.mvs_runtime import DEFAULT_MVS_PYTHON_DIR, ensure_mvs_native_libraries

DEFAULT_INSTALL_ROOT = "/opt/colony_system"
DEFAULT_MODBUS_PORT = "/dev/ttyUSB0"

__all__ = (
    "DEFAULT_INSTALL_ROOT",
    "DEFAULT_MODBUS_PORT",
    "DEFAULT_MVS_PYTHON_DIR",
    "ensure_mvs_native_libraries",
)
