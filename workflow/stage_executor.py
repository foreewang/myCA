"""
位移台执行器
输入是目标坐标，
输出是这次运动的执行结果
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Dict, Any

# 约定项目根目录为当前脚本所在目录的上两级
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MOTION_DIR = PROJECT_ROOT / "devices" / "motion"

# 为兼容“直接运行脚本”的场景，将底层运动控制模块目录加入导入路径
if str(MOTION_DIR) not in sys.path:
    sys.path.insert(0, str(MOTION_DIR))

from modbus import ModbusRTUClient  # type: ignore
from MotorManager import MotorManager  # type: ignore


def snapshot_axis(motor: MotorManager, axis_name: str) -> Dict[str, Any]:
    """
    读取单个轴的当前位置和状态字快照。

    参数
    ----
    motor : MotorManager
        对应某个轴的电机管理对象。
    axis_name : str
        轴名称标记，通常为 "x" 或 "y"。

    返回
    ----
    Dict[str, Any]
        当前轴的快照信息，包括：
        - axis: 轴名称
        - slave: 从站地址
        - current_pos: 当前实际位置
        - statusword: 当前状态字

    说明
    ----
    这个函数的作用不是控制运动，而是“记录当前状态”。
    常用于：
    - 运动前后状态对比
    - 日志记录
    - 排查电机是否到位、是否有异常状态
    """
    pos = motor.client._read_32bit(motor.slave, motor.client.REG_CURRENT_POS)
    sw = motor.client._read_statusword(motor.slave)
    return {
        "axis": axis_name,
        "slave": motor.slave,
        "current_pos": pos,
        "statusword": sw,
    }


def snapshot_xy(x_motor: MotorManager, y_motor: MotorManager) -> Dict[str, Any]:
    """
    同时读取 X/Y 两个轴的状态快照。

    参数
    ----
    x_motor : MotorManager
        X 轴电机对象。
    y_motor : MotorManager
        Y 轴电机对象。

    返回
    ----
    Dict[str, Any]
        包含 x 和 y 两个轴当前状态的字典。

    说明
    ----
    这是一个组合函数，用于把双轴状态统一打包，
    方便在运动前后做整体对比。
    """
    return {
        "x": snapshot_axis(x_motor, "x"),
        "y": snapshot_axis(y_motor, "y"),
    }


def move_to_absolute(
    *,
    port: str,
    x_target: int,
    y_target: int,
    profile_vel: int,
    profile_acc: int,
    profile_dec: int,
    x_slave: int = 1,
    y_slave: int = 2,
    baudrate: int = 115200,
    settle_s: float = 0.8,
) -> Dict[str, Any]:
    """
    控制 X/Y 两个轴移动到指定绝对位置，并返回运动结果。

    参数
    ----
    port : str
        串口号，例如 "COM3"。
    x_target : int
        X 轴目标绝对位置。
    y_target : int
        Y 轴目标绝对位置。
    profile_vel : int
        轮廓速度。
    profile_acc : int
        轮廓加速度。
    profile_dec : int
        轮廓减速度。
    x_slave : int, optional
        X 轴从站地址，默认 1。
    y_slave : int, optional
        Y 轴从站地址，默认 2。
    baudrate : int, optional
        串口波特率，默认 115200。
    settle_s : float, optional
        指令下发后等待系统稳定的时间，单位秒。

    返回
    ----
    Dict[str, Any]
        本次移动的完整结果，包括：
        - target: 目标坐标
        - before: 运动前双轴状态
        - move_result: 下发运动命令后的返回值
        - after: 等待稳定后的双轴状态
        - err_to_target: 实际位置相对目标位置的误差

    处理流程
    --------
    1. 打开 Modbus 串口连接；
    2. 分别创建 X/Y 轴的 MotorManager；
    3. 记录运动前状态；
    4. 向两个轴下发绝对位置运动命令；
    5. 等待电机和机械系统稳定；
    6. 再次读取运动后状态；
    7. 计算当前位置与目标位置之间的误差；
    8. 返回完整运动结果。

    说明
    ----
    这个函数是上层 workflow 调用的“位移执行入口”。
    它不负责决定应该去哪里，只负责：
    - 接收目标位置
    - 执行移动
    - 返回前后状态和误差

    也就是说：
    scan_planner 决定“目标点”
    这个函数负责“把电机移动到那个点”
    """
    with ModbusRTUClient(port=port, baudrate=baudrate) as client:
        x_motor = MotorManager(client, slave=x_slave)
        y_motor = MotorManager(client, slave=y_slave)

        # 记录运动前状态，便于后续做位置变化和状态变化对比
        before = snapshot_xy(x_motor, y_motor)

        # 分别向 X/Y 轴下发绝对位置运动命令
        x_diff = x_motor.pp_absolute_move(
            target_pos=int(x_target),
            profile_vel=int(profile_vel),
            profile_acc=int(profile_acc),
            profile_dec=int(profile_dec),
        )
        y_diff = y_motor.pp_absolute_move(
            target_pos=int(y_target),
            profile_vel=int(profile_vel),
            profile_acc=int(profile_acc),
            profile_dec=int(profile_dec),
        )

        # 等待机械系统稳定后再读取位置，避免刚下发命令就取值导致结果不准
        time.sleep(settle_s)

        # 记录运动后状态
        after = snapshot_xy(x_motor, y_motor)

        # 计算当前位置相对于目标位置的误差
        err_to_target = {
            "x": int(after["x"]["current_pos"]) - int(x_target),
            "y": int(after["y"]["current_pos"]) - int(y_target),
        }

        return {
            "target": {"x": int(x_target), "y": int(y_target)},
            "before": before,
            "move_result": {"x_diff": x_diff, "y_diff": y_diff},
            "after": after,
            "err_to_target": err_to_target,
        }