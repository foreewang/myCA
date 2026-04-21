"""
读取 task
读取 camera.yaml
读取 objectives.yaml
读取 plates.yaml
根据 task 里写的 plate_type 和 objective
去配置文件里找到对应那一段
最后拼成一个 ctx
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml


def load_yaml(path: str | Path) -> Dict[str, Any]:
    """
    读取 YAML 文件并返回字典。

    参数
    ----
    path : str | Path
        YAML 文件路径。

    返回
    ----
    Dict[str, Any]
        解析后的配置字典。
        若 YAML 文件为空，则返回空字典而不是 None。

    说明
    ----
    这里做了一层简单封装，统一项目中 YAML 配置文件的读取方式，
    避免调用方每次都重复写 Path 转换、文件打开和 safe_load 逻辑。
    """
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
    """
    加载一次任务运行所需的全部配置，并组装成统一上下文。

    参数
    ----
    task_path : str | Path
        任务文件路径，通常是 task.yaml。
    camera_path : str | Path | None, optional
        相机配置文件路径。若不传，则默认使用 project_root/config/camera.yaml。
    objectives_path : str | Path | None, optional
        物镜配置文件路径。若不传，则默认使用 project_root/config/objectives.yaml。
    plates_path : str | Path | None, optional
        培养板配置文件路径。若不传，则默认使用 project_root/config/plates.yaml。

    返回
    ----
    Dict[str, Any]
        运行时上下文字典，包含：
        - task: 当前任务配置
        - camera: 当前相机配置
        - objective: 当前任务所选物镜配置
        - plate: 当前任务所选板型配置
        - raw: 原始完整配置内容
        - paths: 本次实际使用的配置文件路径

    异常
    ----
    KeyError
        当配置文件缺少必要顶层字段，或任务中引用了不存在的板型/物镜时抛出。

    说明
    ----
    这个函数的核心作用是：
    把“分散在多个 yaml 文件里的配置”整合成一份运行时可直接使用的上下文。

    上层流程不需要再分别关心：
    - task 在哪里读
    - camera 在哪里读
    - 当前物镜对应哪一段配置
    - 当前板型对应哪一段配置

    只要调用一次这个函数，就能拿到后续流程需要的全部核心配置。
    """
    # 统一转成绝对路径，避免后续路径依赖当前工作目录
    task_path = Path(task_path).resolve()

    # 默认假设任务文件位于 project_root/tasks/ 下，
    # 因此 project_root = task_path.parent.parent
    project_root = task_path.parent.parent
    config_dir = project_root / "config"

    # 若调用方未显式传入路径，则使用项目默认配置目录下的标准文件
    if camera_path is None:
        camera_path = config_dir / "camera.yaml"
    if objectives_path is None:
        objectives_path = config_dir / "objectives.yaml"
    if plates_path is None:
        plates_path = config_dir / "plates.yaml"

    # 读取所有配置文件
    task_cfg = load_yaml(task_path)
    camera_cfg = load_yaml(camera_path)
    objectives_cfg = load_yaml(objectives_path)
    plates_cfg = load_yaml(plates_path)

    # 校验各配置文件是否包含预期顶层字段。
    # 这样可以尽早暴露配置结构错误，而不是等到后续流程中隐式报错。
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

    # 检查任务中声明的板型和物镜，是否真的存在于对应配置文件中。
    if plate_type not in plates_cfg["plates"]:
        raise KeyError(f"plates.yaml 中不存在板型: {plate_type}")
    if objective_name not in objectives_cfg["objectives"]:
        raise KeyError(f"objectives.yaml 中不存在物镜: {objective_name}")

    # 返回统一运行时上下文：
    # - task / camera / objective / plate 是后续流程最常用的“当前有效配置”
    # - raw 保留完整原始配置，便于调试或后续扩展
    # - paths 记录本次实际使用的配置文件路径，便于排查配置来源
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