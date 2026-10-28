"""Shared scan-mode resolution and overlap compatibility rules."""
from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any


def resolve_scan_mode(objective_name: str, requested: str | None = None) -> str:
    """Use five-point observation only for 4x; retain explicit full coverage."""
    mode = requested if requested is not None else (
        "five_point" if objective_name == "4x" else "full_coverage"
    )
    if not isinstance(mode, str) or mode not in ("five_point", "full_coverage"):
        raise ValueError("scan.mode 必须为 five_point 或 full_coverage")
    if mode == "five_point" and objective_name != "4x":
        raise ValueError("scan.mode=five_point 仅支持 4x 物镜")
    return mode


def has_overlapping_views(scan_config: Mapping[str, Any]) -> bool:
    """Prefer measured geometry; old plans fall back to their overlap setting."""
    if "has_overlapping_views" in scan_config:
        value = scan_config["has_overlapping_views"]
        if not isinstance(value, bool):
            raise ValueError("scan_config.has_overlapping_views 必须为 bool")
        return value
    value = scan_config.get("overlap", 0.0)
    if value is None:
        value = 0.0
    if isinstance(value, bool):
        raise ValueError("scan_config.overlap 必须为有限数字")
    try:
        overlap = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("scan_config.overlap 必须为有限数字") from exc
    if not isfinite(overlap):
        raise ValueError("scan_config.overlap 必须为有限数字")
    return overlap > 0.0
