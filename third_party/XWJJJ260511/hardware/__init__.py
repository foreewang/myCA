# 硬件抽象层统一出口：外部可以从 hardware 直接导入常用接口。

# 导出电机抽象类和虚拟电机。
from .motor import MotorBase, VirtualMotor
# 导出相机抽象类和虚拟相机。
from .camera import CameraBase, VideoVirtualCamera

# __all__ 控制 from hardware import * 时暴露哪些名字。
__all__ = [
    # 电机统一接口。
    "MotorBase",
    # 不控制真实硬件的虚拟电机。
    "VirtualMotor",
    # 相机统一接口。
    "CameraBase",
    # 用视频/图片模拟相机的虚拟相机。
    "VideoVirtualCamera",
]
