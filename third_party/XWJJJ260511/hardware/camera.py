"""
相机/图像采集接口。

- CameraBase: 抽象基类，真实相机实现此类即可接入。
- VideoVirtualCamera: 用视频或单帧图像模拟“不同对焦位置”的成像，用于无硬件时测试。
"""

# ABC/abstractmethod 用于定义相机抽象接口。
from abc import ABC, abstractmethod
# Optional 用于标注可以为 None 的参数或成员变量。
from typing import Optional

# OpenCV 用于读视频、读图片、跳帧和高斯模糊。
import cv2
# numpy 用于创建和处理图像数组。
import numpy as np


class CameraBase(ABC):
    """图像采集抽象接口。真实相机需实现 capture()。"""

    # 所有相机实现都必须提供 capture 方法。
    @abstractmethod
    def capture(self) -> np.ndarray:
        """采集一帧图像，返回 BGR 的 numpy 数组 (H, W, 3)，uint8。"""
        # 抽象方法只定义接口，不写具体采图逻辑。
        pass


class VideoVirtualCamera(CameraBase):
    """
    虚拟相机：用视频或单张图模拟对焦变化。

    两种模式：
    1) 视频对焦 sweep：传入视频路径，将“电机位置”映射为帧序号，
       即不同位置 = 不同帧（适合用一段对焦过程录制的视频测试）。
    2) 单图 + 模糊仿真：用一张图或视频的第一帧，根据与“最佳位置”的距离施加高斯模糊，
       模拟清晰度随位置的单峰变化，无需真实对焦视频。
    """

    def __init__(
        self,
        source: str,
        mode: str = "blur",
        virtual_best_position: Optional[float] = None,
        position_range: Optional[tuple] = None,
    ):
        """
        source: 视频文件路径或图像路径；若为 "" 则使用内置生成的灰度图（仅做演示）。
        mode: "sweep" = 位置对应帧序号；"blur" = 单图 + 按位置模糊。
        virtual_best_position: 仅在 blur 模式下有效，表示“最清晰”的虚拟位置。
        position_range: (min_pos, max_pos)，blur 模式下用于计算模糊强度范围。
        """
        # 保存虚拟相机模式：sweep 或 blur。
        self._mode = mode
        # 预留视频对象；当前实现多数时候按需打开视频。
        self._cap: Optional[cv2.VideoCapture] = None
        # 保存参考图像，blur 模式会基于它生成不同模糊程度的图。
        self._reference_image: Optional[np.ndarray] = None
        # 当前虚拟电机位置，set_position 会更新它。
        self._current_position: float = 0.0
        # 视频帧率；图片或内置图时为 0。
        self._fps: float = 0.0
        # 视频总帧数；图片或内置图时为 1。
        self._frame_count: int = 1

        # 如果外部没有给范围，就使用默认虚拟范围。
        if position_range is None:
            position_range = (0.0, 10000.0)
        # 保存虚拟最小位置和最大位置。
        self._min_pos, self._max_pos = position_range
        # 保存虚拟最佳焦点位置。
        self._best_pos = virtual_best_position
        # 如果没指定最佳位置，就默认在范围中间。
        if self._best_pos is None:
            self._best_pos = (self._min_pos + self._max_pos) / 2.0

        # 保存输入资源路径。
        self._source = source

        # 没有传文件时，生成一张简单图用于算法演示。
        if not source:
            # 无文件时：生成一张简单图用于演示
            h, w = 480, 640
            # 创建黑底 BGR 图像。
            img = np.zeros((h, w, 3), dtype=np.uint8)
            # 填充灰色背景。
            img[:] = (180, 180, 180)
            # 画一个圆作为可对焦细节。
            cv2.circle(img, (w // 2, h // 2), 80, (80, 80, 80), 2)
            # 保存为参考图。
            self._reference_image = img
            self._frame_count = 1
            self._fps = 0.0
            return

        # 尝试按视频打开
        cap = cv2.VideoCapture(source)
        # 如果 OpenCV 能打开，说明 source 大概率是视频。
        if cap.isOpened():
            # 读取视频总帧数，失败时至少设为 1。
            self._frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
            # 读取视频 FPS，失败时设为 0。
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            self._fps = fps if fps > 0 else 0.0
            # blur 模式只需要第一帧作为参考图。
            if mode == "blur":
                ret, frame = cap.read()
                if ret:
                    self._reference_image = frame.copy()
                else:
                    self._reference_image = np.zeros((480, 640, 3), dtype=np.uint8)
            # sweep 模式把位置映射到视频帧序号。
            elif mode == "sweep":
                # sweep 模式下，最大位置默认改成最后一帧序号。
                self._max_pos = max(self._min_pos, self._frame_count - 1)
                ret, frame = cap.read()
                self._reference_image = frame.copy() if ret else np.zeros((480, 640, 3), dtype=np.uint8)
            # 释放临时打开的视频。
            cap.release()
            return

        # 按图像打开
        img = cv2.imread(source)
        # 如果能读到图像，就把它作为参考图。
        if img is not None:
            self._reference_image = img
            self._frame_count = 1
            self._fps = 0.0
            return

        # 失败则用灰图
        # 文件既不是视频也不是图片时，降级为黑图，避免测试流程崩掉。
        self._reference_image = np.zeros((480, 640, 3), dtype=np.uint8)
        self._frame_count = 1
        self._fps = 0.0

    def set_position(self, position: float) -> None:
        """由外部设置当前“虚拟电机位置”，下次 capture 将据此返回对应画面。"""
        # 把虚拟位置限制在允许范围内。
        self._current_position = max(self._min_pos, min(self._max_pos, position))

    def capture(self) -> np.ndarray:
        # sweep 模式从视频指定帧取图。
        if self._mode == "sweep":
            return self._capture_sweep()
        # 默认 blur 模式按位置生成不同模糊程度的图。
        return self._capture_blur()

    def _capture_sweep(self) -> np.ndarray:
        """位置 = 帧序号：打开视频并取该帧。"""
        # 没有 source 时直接返回参考图。
        if not getattr(self, "_source", ""):
            return self._reference_image.copy() if self._reference_image is not None else np.zeros((480, 640, 3), dtype=np.uint8)
        # 把当前位置转换成视频帧号，并限制在合法范围内。
        frame_idx = int(max(0, min(self._current_position, self._frame_count - 1)))
        # 打开视频文件。
        cap = cv2.VideoCapture(self._source)
        # 打不开时返回参考图或黑图。
        if not cap.isOpened():
            return self._reference_image.copy() if self._reference_image is not None else np.zeros((480, 640, 3), dtype=np.uint8)
        # 跳到目标帧。
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        # 读取目标帧。
        ret, frame = cap.read()
        # 释放视频资源。
        cap.release()
        # 成功读到帧就返回。
        if ret:
            return frame
        # 读取失败时降级返回参考图或黑图。
        return self._reference_image.copy() if self._reference_image is not None else np.zeros((480, 640, 3), dtype=np.uint8)

    def _capture_blur(self) -> np.ndarray:
        """根据当前位置与 best_pos 的距离施加高斯模糊，模拟清晰度单峰。"""
        # 取参考图。
        img = self._reference_image
        # 没有参考图时返回黑图。
        if img is None:
            return np.zeros((480, 640, 3), dtype=np.uint8)
        # 复制一份，避免修改原始参考图。
        img = img.copy()
        # 当前位置离最佳位置越远，模糊越强。
        dist = abs(self._current_position - self._best_pos)
        # 范围跨度至少给一个很小值，防止除零。
        span = max(1e-6, self._max_pos - self._min_pos)
        # 归一化距离 0~1 对应 sigma 0~约 30
        # sigma 是高斯模糊强度。
        sigma = 30.0 * (dist / span)
        # 根据 sigma 估算卷积核大小，并确保是奇数。
        ksize = int(6 * sigma + 1) | 1
        # 限制卷积核大小，避免太小没效果或太大太慢。
        ksize = max(3, min(ksize, 51))
        # 返回模糊后的虚拟图像。
        return cv2.GaussianBlur(img, (ksize, ksize), sigma)

    def get_range(self) -> tuple:
        # 返回虚拟相机对应的虚拟位置范围。
        return (self._min_pos, self._max_pos)

    def get_fps(self) -> float:
        """若 source 为视频则返回 FPS，否则为 0。"""
        return float(self._fps)

    def get_frame_count(self) -> int:
        """若 source 为视频则返回总帧数，否则为 1。"""
        return int(self._frame_count)

    def close(self) -> None:
        # 如果曾经持有视频对象，释放它。
        if self._cap is not None:
            self._cap.release()
            self._cap = None


def open_video_sweep_camera(
    video_path: str,
    position_range: Optional[tuple] = None,
) -> "VideoVirtualCamera":
    """
    打开“视频对焦 sweep”虚拟相机：电机位置 = 视频帧序号。
    适合用一段实际对焦过程录制的视频做算法测试。
    """
    # 创建 sweep 模式虚拟相机。
    return VideoVirtualCamera(
        source=video_path,
        mode="sweep",
        position_range=position_range,
    )


def open_blur_virtual_camera(
    image_or_video_path: str = "",
    best_position: Optional[float] = None,
    position_range: Optional[tuple] = None,
) -> "VideoVirtualCamera":
    """
    打开“单图+模糊”虚拟相机：无真实对焦视频时也可测试搜索算法。
    """
    # 创建 blur 模式虚拟相机。
    return VideoVirtualCamera(
        source=image_or_video_path,
        mode="blur",
        virtual_best_position=best_position,
        position_range=position_range,
    )
