# -*- coding: utf-8 -*-
"""单轴电机状态、模式与运动控制封装。"""

from __future__ import annotations

import logging
from typing import Optional

try:
    from .modbus import ModbusRTUClient
except ImportError:  # 兼容以旧版顶层模块方式导入。
    from modbus import ModbusRTUClient


logger = logging.getLogger(__name__)


class MotorManager:
    """管理一个 Modbus 从站电机的模式、使能状态和运动命令。"""

    # CiA 402 操作模式代码。
    MODE_PROFILE_POSITION = 0x01
    MODE_VELOCITY = 0x02
    MODE_PROFILE_VELOCITY = 0x03

    def __init__(self, client: ModbusRTUClient, slave: int):
        self.client = client
        self.slave = slave

    def _read_statusword(self) -> Optional[int]:
        """读取状态字；通信失败时返回 None。"""
        return self.client._read_statusword(self.slave)

    def get_current_position(self) -> Optional[int]:
        """只读当前位置；通信失败时返回 None。"""
        return self.client._read_32bit(
            self.slave,
            ModbusRTUClient.REG_CURRENT_POS,
        )

    def _is_enabled(self) -> bool:
        """返回电机是否处于 Operation Enabled 状态。"""
        status = self._read_statusword()
        return status is not None and bool(status & ModbusRTUClient.STAT_OPERATION_ENABLED)

    def _get_current_mode(self) -> Optional[int]:
        """读取当前操作模式（6061h）；通信失败时返回 None。"""
        registers = self.client.read_holding_registers(
            self.slave,
            ModbusRTUClient.REG_MODE_DISPLAY,
            1,
        )
        return registers[0] if registers else None

    def _ensure_mode_and_enable(self, target_mode: int, auto_enable: bool = True) -> bool:
        """确保电机处于目标模式，并按需完成故障复位和使能。"""
        current_mode = self._get_current_mode()
        if current_mode is None:
            logger.error("从站 %s 无法读取当前模式", self.slave)
            return False

        if current_mode == target_mode:
            if auto_enable:
                logger.info("从站 %s 模式已为 %s，尝试使能", self.slave, hex(target_mode))
                return self.client.enable_motor(self.slave)
            return True

        logger.info(
            "从站 %s 从模式 %s 切换至 %s",
            self.slave,
            hex(current_mode),
            hex(target_mode),
        )
        return self.client.switch_mode(
            self.slave,
            target_mode,
            auto_enable=auto_enable,
        )

    def pp_absolute_move(
        self,
        target_pos: int,
        profile_vel: int,
        profile_acc: int,
        profile_dec: int,
        timeout: float = 120.0,
    ) -> Optional[int]:
        """在轮廓位置模式下移动到绝对位置，并返回位置误差。"""
        if not self._ensure_mode_and_enable(self.MODE_PROFILE_POSITION, True):
            return None
        return self.client.move_absolute_pp(
            self.slave,
            target_pos,
            profile_vel,
            profile_acc,
            profile_dec,
            timeout,
        )

    def pp_relative_move(
        self,
        offset: int,
        profile_vel: int,
        profile_acc: int,
        profile_dec: int,
        timeout: float = 120.0,
    ) -> Optional[int]:
        """在轮廓位置模式下执行相对位移，并返回位置误差。"""
        if not self._ensure_mode_and_enable(self.MODE_PROFILE_POSITION, True):
            return None
        return self.client.move_relative_pp(
            self.slave,
            offset,
            profile_vel,
            profile_acc,
            profile_dec,
            timeout,
        )

    def pv_start(self, target_velocity: int, profile_acc: int, profile_dec: int) -> bool:
        """启动轮廓速度模式。"""
        if not self._ensure_mode_and_enable(self.MODE_PROFILE_VELOCITY, False):
            return False
        return self.client.start_velocity_mode(
            self.slave,
            target_velocity,
            profile_acc,
            profile_dec,
        )

    def pv_stop(self) -> bool:
        """将轮廓速度模式的目标速度设为零。"""
        return self.client.stop_velocity(self.slave)

    def vl_start(
        self,
        target_velocity: int,
        acceleration: int,
        acc_time: int,
        deceleration: int,
        dec_time: int,
    ) -> bool:
        """启动驱动器自定义速度模式。"""
        if not self._ensure_mode_and_enable(self.MODE_VELOCITY, True):
            return False
        return self.client.start_speed_mode(
            self.slave,
            target_velocity,
            acceleration,
            acc_time,
            deceleration,
            dec_time,
        )

    def vl_stop(self) -> bool:
        """停止驱动器自定义速度模式。"""
        return self.client.quick_stop(self.slave)
