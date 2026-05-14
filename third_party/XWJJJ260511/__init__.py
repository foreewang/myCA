"""YAML-configured realtime autofocus package.

这个包把自动对焦主流程、相机、电机和配置入口放在同一个目录里。
外部程序通常从 run.py 或 autofocus_api.py 调用，不需要直接改这里。
"""

from .run import run_autofocus_from_config as run_autofocus


__all__ = ["run_autofocus"]
