from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from third_party.XWJJJ260511 import run_autofocus


def _project_root_from_ctx(ctx: Dict[str, Any]) -> Path:
    # autofocus_executor.py 位于 workflow/ 下，项目根目录为其上一级。
    return Path(__file__).resolve().parent.parent


def _resolve_config_path(ctx: Dict[str, Any], config_path: str | Path) -> Path:
    path = Path(config_path)
    if path.is_absolute():
        return path
    return _project_root_from_ctx(ctx) / path


def execute_autofocus_for_task(
    ctx: Dict[str, Any],
    task_cfg: Dict[str, Any],
    objective_result: Dict[str, Any],
    autofocus_cfg: Dict[str, Any],
    *,
    trigger_reason: str | None = None,
) -> Dict[str, Any]:
    objective_name = str(task_cfg.get("objective") or "").strip()
    if not objective_name:
        raise ValueError("autofocus 需要 task.objective 非空")

    raw_config_path = autofocus_cfg.get("config_path")
    if not raw_config_path:
        raise ValueError("自动调焦已被触发，但未提供 autofocus.config_path，也未找到本地默认配置路径")

    config_path = _resolve_config_path(ctx, raw_config_path)
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
        "trigger_reason": trigger_reason,
        "triggered_by_objective_switch": bool(objective_result.get("switched", False)),
        "config_path": str(config_path),
        "best_pos": float(result.best_pos),
        "best_value": float(result.best_value),
        "output_path": str(output_path) if output_path else None,
        "elapsed_sec": float(result.elapsed_sec),
        "focus_log_count": len(focus_log),
    }
