from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml


def load_yaml(path: str | Path) -> Dict[str, Any]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def load_runtime_context(
    task_path: str | Path,
    camera_path: str | Path | None = None,
    objectives_path: str | Path | None = None,
    plates_path: str | Path | None = None,
) -> Dict[str, Any]:
    task_path = Path(task_path).resolve()

    project_root = task_path.parent.parent
    config_dir = project_root / "config"

    if camera_path is None:
        camera_path = config_dir / "camera.yaml"
    if objectives_path is None:
        objectives_path = config_dir / "objectives.yaml"
    if plates_path is None:
        plates_path = config_dir / "plates.yaml"

    task_cfg = load_yaml(task_path)
    camera_cfg = load_yaml(camera_path)
    objectives_cfg = load_yaml(objectives_path)
    plates_cfg = load_yaml(plates_path)

    if "task" not in task_cfg:
        raise KeyError(f"task_template.yaml 缺少顶层字段 'task': {task_path}")
    if "camera" not in camera_cfg:
        raise KeyError(f"camera.yaml 缺少顶层字段 'camera': {camera_path}")
    if "objectives" not in objectives_cfg:
        raise KeyError(f"objectives.yaml 缺少顶层字段 'objectives': {objectives_path}")
    if "plates" not in plates_cfg:
        raise KeyError(f"plates.yaml 缺少顶层字段 'plates': {plates_path}")

    task = task_cfg["task"]
    plate_type = task["plate_type"]
    objective_name = task["objective"]

    if plate_type not in plates_cfg["plates"]:
        raise KeyError(f"plates.yaml 中不存在板型: {plate_type}")
    if objective_name not in objectives_cfg["objectives"]:
        raise KeyError(f"objectives.yaml 中不存在物镜: {objective_name}")

    return {
        "task": task,
        "camera": camera_cfg["camera"],
        "objective": objectives_cfg["objectives"][objective_name],
        "plate": plates_cfg["plates"][plate_type],
        "raw": {
            "task_cfg": task_cfg,
            "camera_cfg": camera_cfg,
            "objectives_cfg": objectives_cfg,
            "plates_cfg": plates_cfg,
        },
        "paths": {
            "task_path": str(task_path),
            "camera_path": str(Path(camera_path).resolve()),
            "objectives_path": str(Path(objectives_path).resolve()),
            "plates_path": str(Path(plates_path).resolve()),
        },
    }