from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from third_party.XWJJJ260511 import run_autofocus


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def execute_autofocus_for_task(
    ctx: Dict[str, Any],
    task_cfg: Dict[str, Any],
    objective_result: Dict[str, Any],
    autofocus_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    objective_name = str(task_cfg.get("objective") or "").strip()
    if not objective_name:
        raise ValueError("autofocus 需要 task.objective 非空")

    config_path_value = autofocus_cfg.get("config_path")
    if not config_path_value:
        raise ValueError("autofocus 启用时，必须提供 autofocus.config_path 或本地 config/autofocus.yaml")

    config_path = _resolve_path(config_path_value)
    if not config_path.exists():
        raise FileNotFoundError(f"未找到 autofocus 配置文件: {config_path}")

    result = run_autofocus(
        str(config_path),
        objective=objective_name,
    )

    focus_log = getattr(result, "focus_log", None) or []
    output_path = getattr(result, "output_path", None)

    return {
        "status": "success",
        "objective_name": objective_name,
        "config_path": str(config_path),
        "best_pos": float(getattr(result, "best_pos")),
        "best_value": float(getattr(result, "best_value")),
        "output_path": str(output_path) if output_path else None,
        "elapsed_sec": float(getattr(result, "elapsed_sec")),
        "focus_log_count": len(focus_log),
        "triggered_by_objective_switch": bool(objective_result.get("switched", False)),
    }

