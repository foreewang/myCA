"""Linux industrial-PC defaults for serial ports, install root, and MVS paths."""
from __future__ import annotations

from typing import Any, Dict, Mapping

from devices.mvs_runtime import DEFAULT_MVS_PYTHON_DIR, ensure_mvs_native_libraries

DEFAULT_INSTALL_ROOT = "/opt/colony_system"
DEFAULT_MODBUS_PORT = "/dev/ttyUSB0"
DEFAULT_SCAN_OVERLAP = 0
DEFAULT_SCAN_SETTLE_S = 0.5
DEFAULT_PROFILE_VEL = 500000
DEFAULT_PROFILE_ACC = 500000
DEFAULT_PROFILE_DEC = 500000

__all__ = (
    "DEFAULT_INSTALL_ROOT",
    "DEFAULT_MODBUS_PORT",
    "DEFAULT_SCAN_OVERLAP",
    "DEFAULT_SCAN_SETTLE_S",
    "DEFAULT_PROFILE_VEL",
    "DEFAULT_PROFILE_ACC",
    "DEFAULT_PROFILE_DEC",
    "DEFAULT_MVS_PYTHON_DIR",
    "ensure_mvs_native_libraries",
    "apply_motion_profile_defaults",
)


def apply_motion_profile_defaults(motion: Mapping[str, Any] | None) -> Dict[str, Any]:
    cfg = dict(motion or {})
    if cfg.get("profile_vel") is None:
        cfg["profile_vel"] = DEFAULT_PROFILE_VEL
    if cfg.get("profile_acc") is None:
        cfg["profile_acc"] = DEFAULT_PROFILE_ACC
    if cfg.get("profile_dec") is None:
        cfg["profile_dec"] = DEFAULT_PROFILE_DEC
    return cfg
