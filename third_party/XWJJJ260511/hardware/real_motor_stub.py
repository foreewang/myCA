"""
真实聚焦电机占位实现。

继承 MotorBase，对接实际硬件（步进/伺服/串口/Modbus 等）。
在 run_autofocus_realtime.py 中替换 VirtualMotor 为本模块中的实现即可。
"""

# Tuple 用来标注 get_range 返回两个数字。
from typing import Tuple

# RealMotor 必须实现 MotorBase 定义的三个方法。
from .motor import MotorBase


class RealMotor(MotorBase):
    """
    真实电机占位：请根据你的硬件替换内部实现。
    - move_to: 发指令到电机驱动，阻塞直到到位或超时。
    - get_position: 从编码器或驱动读当前步数/角度。
    - get_range: 返回允许的最小/最大位置（与机械限位一致）。
    """

    def __init__(self, min_pos: float = 0.0, max_pos: float = 10000.0, **kwargs):
        # 真实电机允许的最小软件位置。
        self._min = min_pos
        # 真实电机允许的最大软件位置。
        self._max = max_pos
        # 占位实现里先把当前位置设为最小值。
        self._position = min_pos
        # kwargs: 串口名、从站号、每圈步数等，按实际硬件填写

    def move_to(self, position: float) -> None:
        # 先做软件限位，防止目标位置超出允许范围。
        target = max(self._min, min(self._max, position))
        # TODO: 调用硬件 API，例如：
        # self._serial.write(f"GOTO {target}\n")
        # self._wait_until_stop()
        # 当前文件只是占位版本，所以这里只更新内存位置。
        self._position = target

    def get_position(self) -> float:
        # TODO: 从硬件读当前值，例如：
        # return self._read_encoder()
        # 占位版本没有硬件反馈，返回内存中保存的位置。
        return self._position

    def get_range(self) -> Tuple[float, float]:
        # 返回软件允许的最小/最大位置。
        return (self._min, self._max)
