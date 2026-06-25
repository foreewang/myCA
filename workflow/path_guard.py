"""
路径安全层，统一处理API请求里传进来的文件路径，
确保它们都在允许的目录下，防止路径穿越攻击。
定义项目允许访问的根目录
校验路径是否在允许目录内
解析配置文件路径
解析输出文件路径
规范化任务体里的嵌套路径
规范化 /api/tasks/execute 的路径参数
路径安全异常
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_ROOT = PROJECT_ROOT / "config"
DATA_ROOT = PROJECT_ROOT / "data"
OUTPUTS_ROOT = PROJECT_ROOT / "outputs"


class PathGuardError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        *,
        log_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.error_code = str(error_code)
        self.message = str(message)
        self.log_detail = log_detail


def safe_str_path(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_path_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def resolve_allowed_path(value: Any, allowed_roots: tuple[Path, ...], field_name: str) -> str | None:
    raw = safe_str_path(value)
    if raw is None:
        return None

    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve(strict=False)
    resolved_roots = tuple(root.resolve(strict=False) for root in allowed_roots)
    if not any(is_path_within(resolved, root) for root in resolved_roots):
        allowed = ", ".join(str(root) for root in resolved_roots)
        raise PathGuardError(
            400,
            "PATH_OUT_OF_ALLOWED_ROOT",
            "请求路径不在允许目录内",
            log_detail=f"{field_name} resolved={resolved} allowed={allowed}",
        )
    return str(resolved)


def resolve_config_path(value: Any, field_name: str) -> str | None:
    return resolve_allowed_path(value, (CONFIG_ROOT,), field_name)


def resolve_output_path(value: Any, field_name: str) -> str | None:
    return resolve_allowed_path(value, (DATA_ROOT, OUTPUTS_ROOT), field_name)


def normalize_nested_path(task: Dict[str, Any], keys: tuple[str, ...], field_name: str) -> None:
    node: Any = task
    for key in keys[:-1]:
        if not isinstance(node, dict):
            return
        node = node.get(key)
    if not isinstance(node, dict):
        return
    leaf = keys[-1]
    if leaf in node and node[leaf] is not None:
        node[leaf] = resolve_output_path(node[leaf], field_name)


def normalize_task_paths(task: Dict[str, Any]) -> Dict[str, Any]:
    normalized = copy.deepcopy(task)
    for keys in (
        ("capture", "save_dir"),
        ("scan", "output_json"),
        ("detect", "output_json"),
        ("detect", "input_scan_result_json"),
        ("compensate", "input_detect_json"),
        ("compensate", "output_json"),
        ("compensate", "closed_loop", "save_dir"),
        ("output", "result_json"),
        ("output", "scan_json"),
        ("output", "detect_json"),
        ("output", "compensate_json"),
    ):
        normalize_nested_path(normalized, keys, ".".join(("task", *keys)))
    return normalized


def normalize_execute_task_values(
    *,
    task: Dict[str, Any],
    camera_path: str | None = None,
    objectives_path: str | None = None,
    plates_path: str | None = None,
    dump_json: str | None = None,
) -> Dict[str, Any]:
    return {
        "task": normalize_task_paths(task or {}),
        "camera_path": resolve_config_path(
            camera_path or os.getenv("CAMERA_CONFIG_PATH") or str(CONFIG_ROOT / "camera.yaml"),
            "camera_path",
        ),
        "objectives_path": resolve_config_path(
            objectives_path or os.getenv("OBJECTIVES_CONFIG_PATH") or str(CONFIG_ROOT / "objectives.yaml"),
            "objectives_path",
        ),
        "plates_path": resolve_config_path(
            plates_path or os.getenv("PLATES_CONFIG_PATH") or str(CONFIG_ROOT / "plates.yaml"),
            "plates_path",
        ),
        "dump_json": resolve_output_path(dump_json, "dump_json"),
    }
