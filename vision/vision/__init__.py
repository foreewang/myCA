"""vision 包的对外入口。

上层业务通常只需要调用:
- detect_from_path: 传入图片路径，执行完整检测流程。
- detect_from_gray: 传入内存中的灰度图或彩色图，执行完整检测流程。
- detect_and_refine: 只运行核心检测算法，不负责读写文件。
"""

from .detect_pipeline import detect_and_refine, detect_from_gray, detect_from_path

__all__ = [
    "detect_and_refine",
    "detect_from_gray",
    "detect_from_path",
]
