from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from workflow.align_from_detect import run_align_from_detect_task  # type: ignore


def load_yaml(path: str | Path) -> dict:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True, help="align task yaml")
    args = parser.parse_args()

    cfg = load_yaml(args.task)
    if "task" not in cfg:
        raise KeyError("task yaml 缺少顶层字段 task")

    task = cfg["task"]
    if task.get("task_type") != "align_clone_one_step_from_detect":
        raise ValueError(
            f"当前脚本仅支持 task_type=align_clone_one_step_from_detect，收到: {task.get('task_type')}"
        )

    result = run_align_from_detect_task(task)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
