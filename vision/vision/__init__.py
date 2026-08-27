"""vision 包的对外入口。

默认业务入口是模型化 4x 实例定位。旧规则算法保留 ``legacy_*`` 别名，
只在调用方显式选择 legacy 时使用。
"""

from .instance_pipeline import detect_from_array, detect_from_path, process_image
from .detect_pipeline import (
    detect_and_refine as legacy_detect_and_refine,
    detect_from_gray as legacy_detect_from_gray,
    detect_from_path as legacy_detect_from_path,
)

__all__ = [
    "detect_from_array",
    "detect_from_path",
    "process_image",
    "legacy_detect_and_refine",
    "legacy_detect_from_gray",
    "legacy_detect_from_path",
]
