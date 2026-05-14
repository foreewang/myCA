"""
电机控制接口。

- MotorBase: 抽象基类，真实硬件实现此类即可接入。
- VirtualMotor: 虚拟电机，仅内存记录位置，用于无硬件时测试。
"""

# ABC/abstractmethod 用来定义“必须由子类实现”的抽象接口。
from abc import ABC, abstractmethod
# Tuple 用来标注 get_range 返回两个数字。
from typing import Tuple


class MotorBase(ABC):
    """聚焦旋钮电机抽象接口。真实硬件需实现 move_to / get_position。"""

    # abstractmethod 表示子类必须实现这个方法，否则不能实例化。
    @abstractmethod
    def move_to(self, position: float) -> None:
        """将电机转到指定位置（步数或角度，由实现定义）。"""
        # 抽象方法里不写具体逻辑，只规定接口形状。
        pass

    # 子类必须能返回当前位置。
    @abstractmethod
    def get_position(self) -> float:
        """返回当前电机位置。"""
        # 真实电机通常从驱动器或编码器读取；虚拟电机从内存读取。
        pass

    # 子类必须能返回允许搜索的范围。
    @abstractmethod
    def get_range(self) -> Tuple[float, float]:
        """返回 (最小位置, 最大位置)，对焦搜索将在此范围内进行。"""
        # 注意这里是软件允许范围，不一定等于机械全行程。
        pass


class VirtualMotor(MotorBase):
    """虚拟电机：仅保存当前位置，不驱动任何硬件。"""

    def __init__(self, min_pos: float = 0.0, max_pos: float = 10000.0):
        # 保存虚拟电机最小位置。
        self._min = min_pos
        # 保存虚拟电机最大位置。
        self._max = max_pos
        # 初始位置设为最小位置。
        self._position = min_pos

    def move_to(self, position: float) -> None:
        # 把目标位置限制在允许范围内，然后保存到内存变量。
        self._position = max(self._min, min(self._max, position))

    def get_position(self) -> float:
        # 虚拟电机没有硬件反馈，直接返回内存里的当前位置。
        return self._position

    def get_range(self) -> Tuple[float, float]:
        # 返回虚拟电机允许搜索的范围。
        return (self._min, self._max)
