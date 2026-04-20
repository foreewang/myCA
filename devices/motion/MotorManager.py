# -*- coding: utf-8 -*-
"""
电机动作管理类
=================
为每个电机创建一个独立的管理实体，封装状态检查、模式切换与运动控制。
支持轮廓位置模式(PP)、轮廓速度模式(PV)及通用速度模式(VL)。
"""

import logging
from typing import Optional
import time

# 假设已有 modbus 模块中的 ModbusRTUClient 类
from modbus import ModbusRTUClient

logger = logging.getLogger(__name__)


class MotorManager:
    """
    电机管理器，负责单个电机的状态维护、模式切换和运动控制。
    所有公开方法在执行前都会自动检查电机状态并切换到所需模式。
    """

    # CiA 402 标准操作模式代码
    MODE_PROFILE_POSITION = 0x01      # 轮廓位置模式 (PP)
    MODE_VELOCITY = 0x02              # 速度模式 (VL)
    MODE_PROFILE_VELOCITY = 0x03      # 轮廓速度模式 (PV)

    def __init__(self, client: ModbusRTUClient, slave: int):
        """
        初始化电机管理器。

        :param client: 已连接的 ModbusRTUClient 实例
        :param slave:  电机对应的 Modbus 从站地址
        """
        self.client = client
        self.slave = slave

    # -------------------- 内部状态查询方法 --------------------
    def _read_statusword(self) -> Optional[int]:
        """读取状态字，失败返回 None"""
        return self.client._read_statusword(self.slave)

    def _is_enabled(self) -> bool:
        """检查电机是否处于使能状态 (Operation Enabled)"""
        status = self._read_statusword()
        return status is not None and bool(status & ModbusRTUClient.STAT_OPERATION_ENABLED)

    def _get_current_mode(self) -> Optional[int]:
        """读取当前操作模式 (6061h)，失败返回 None"""
        regs = self.client.read_holding_registers(self.slave, ModbusRTUClient.REG_MODE_DISPLAY, 1)
        return regs[0] if regs else None

    def _ensure_mode_and_enable(self, target_mode: int,auto_enable: bool = True) -> bool:
        """
        确保电机处于指定模式且已使能。
        若存在故障则自动复位；若模式不匹配则自动切换模式（内部处理停止、去使能、切换、重新使能）。
        """
        current_mode = self._get_current_mode()
        if current_mode is None:
            logger.error(f"从站 {self.slave} 无法读取当前模式")
            return False

        if current_mode == target_mode:
            # 模式正确，检查使能状态
            if auto_enable:
                logger.info(f"从站 {self.slave} 模式已为 {hex(target_mode)}，尝试使能")
                return self.client.enable_motor(self.slave)
            else:
                return True
        else:
            # 模式不匹配：直接调用 switch_mode，它会自动处理停止、去使能、切换、重新使能
            logger.info(f"从站 {self.slave} 从模式 {hex(current_mode)} 切换至 {hex(target_mode)}")
            return self.client.switch_mode(self.slave, target_mode, auto_enable=auto_enable)

    # -------------------- 轮廓位置模式 (PP) 公开方法 --------------------
    def pp_absolute_move(
        self,
        target_pos: int,
        profile_vel: int,
        profile_acc: int,
        profile_dec: int,
        timeout: float = 1200.0
    ) -> Optional[int]:
        """
        轮廓位置模式下的绝对运动。
        执行前自动切换到 PP 模式并确保电机使能。

        :param target_pos:   目标绝对位置（脉冲数，32位有符号）
        :param profile_vel:  轮廓速度（脉冲/秒）
        :param profile_acc:  轮廓加速度（脉冲/秒²）
        :param profile_dec:  轮廓减速度（脉冲/秒²）
        :param timeout:      运动超时时间（秒）
        :return:             位置误差（当前位置-指令位置），失败返回 None
        """
        if not self._ensure_mode_and_enable(self.MODE_PROFILE_POSITION,True):
            return None
        return self.client.move_absolute_pp(
            self.slave, target_pos, profile_vel, profile_acc, profile_dec, timeout
        )

    def pp_relative_move(
        self,
        offset: int,
        profile_vel: int,
        profile_acc: int,
        profile_dec: int,
        timeout: float = 1200.0
    ) -> Optional[int]:
        """
        轮廓位置模式下的相对运动。

        :param offset:       相对偏移量（脉冲数，正负代表方向）
        :param profile_vel:  轮廓速度（脉冲/秒）
        :param profile_acc:  轮廓加速度（脉冲/秒²）
        :param profile_dec:  轮廓减速度（脉冲/秒²）
        :param timeout:      运动超时时间（秒）
        :return:             位置误差，失败返回 None
        """
        if not self._ensure_mode_and_enable(self.MODE_PROFILE_POSITION,True):
            return None
        return self.client.move_relative_pp(
            self.slave, offset, profile_vel, profile_acc, profile_dec, timeout
        )

    # -------------------- 轮廓速度模式 (PV) 公开方法 --------------------
    def pv_start(self, target_velocity: int, profile_acc: int, profile_dec: int) -> bool:
        """
        启动轮廓速度模式运行（电机连续运转）。
        自动切换到 PV 模式并使能电机。

        :param target_velocity: 目标速度（脉冲/秒，32位有符号）
        :param profile_acc:     轮廓加速度（脉冲/秒²）
        :param profile_dec:     轮廓减速度（脉冲/秒²）
        :return:                成功返回 True
        """
        if not self._ensure_mode_and_enable(self.MODE_PROFILE_VELOCITY,False):
            return False
        return self.client.start_velocity_mode(self.slave, target_velocity, profile_acc, profile_dec)

    def pv_stop(self) -> bool:
        """
        停止轮廓速度模式运行（将目标速度设为 0，电机减速停止）。
        不需要切换模式，直接发送停止命令。
        """
        return self.client.stop_velocity(self.slave)

    # -------------------- 速度模式 (VM) 公开方法 --------------------
    def vl_start(
        self,
        target_velocity: int,
        acceleration: int,
        acc_time: int,
        deceleration: int,
        dec_time: int
    ) -> bool:
        """
        启动速度模式（VL）运行，使用驱动器自定义的速度控制参数。
        自动切换到 VL 模式并使能电机。

        :param target_velocity: 目标速度（脉冲/秒）
        :param acceleration:    加速度（脉冲/秒²）
        :param acc_time:        加速度时间（驱动器单位，通常为毫秒）
        :param deceleration:    减速度（脉冲/秒²）
        :param dec_time:        减速度时间（驱动器单位）
        :return:                成功返回 True
        """
        if not self._ensure_mode_and_enable(self.MODE_VELOCITY,True):
            return False
        return self.client.start_speed_mode(
            self.slave, target_velocity, acceleration, acc_time, deceleration, dec_time
        )

    def vl_stop(self) -> bool:
        """
        停止速度模式运行。
        :return:             成功返回 True
        """
        return self.client.quick_stop(self.slave)





# x y 电机点位参数
# 12 孔托盘 00 点 xy坐标(rpm)
point_12=(8563500,5755000)
# 12 孔托盘 直径 (0.1mm)
point_12_d = 195
# 12 孔托盘 孔位间隙 (0.1mm)
point_12_gap = 36

# 24 孔托盘 00 点 xy坐标(rpm)
point_24 = (8865800,6185500)
# 24 孔托盘 直径 (0.1 mm)
point_24_d = 137
# 24 孔托盘 孔位间隙 (0.1mm)
point_24_gap = 35

# 48 孔托盘 00 点 xy坐标(rpm)
point_48 = (9412400,5841900)
# 48 孔托盘 直径 (0.1mm)
point_48_d = 93
# 48 孔托盘 孔位间隙 (0.1mm)
point_48_gap= 24

# 0.1mm 对应 14750 rpm 
rpm_mm = 14750

# 镜片电机旋转参数
x4 = 166347
x10 = -165903

# 调教电机点位参数
x4_focal= -2019367
x10_focal = -2053794

# ========== 使用示例 ==========
if __name__ == "__main__":
    # 创建 Modbus 客户端并连接
    with ModbusRTUClient(port="COM3", baudrate=115200) as client:
        # 为从站地址 1 的电机创建管理器
        # x 轴
        x = MotorManager(client, slave=1)
        # y 轴
        y = MotorManager(client,slave=2)
        # 更换物镜镜头
        objective = MotorManager(client,slave=4)
        # 调整焦距
        focus = MotorManager(client,slave=3)
       
        # 更换镜片为 4 倍镜
        # objective.pp_absolute_move(x4,100000,100000,100000)
        # 更换镜片为 10 倍镜
        # objective.pp_absolute_move(x10,100000,100000,100000)

        # 调整焦距为 4 倍镜下指定焦距
        # focus.pp_absolute_move(x4_focal,100000,100000,100000)
        # 调整焦距为 10 倍镜下指定焦距
        # focus.pp_absolute_move(x10_focal,100000,100000,100000)

        # 移动到12 孔托盘 00 点位
        # x.pp_absolute_move(target_pos=point_12[0], profile_vel=500000, profile_acc=100000, profile_dec=100000)
        # y.pp_absolute_move(target_pos=point_12[1], profile_vel=500000, profile_acc=100000, profile_dec=100000)

        # 移动到12 孔托盘 01 点位(y)
        # x.pp_absolute_move(target_pos=point_12[0], profile_vel=500000, profile_acc=100000, profile_dec=100000)
        # y.pp_absolute_move(target_pos=point_12[1] - ((point_12_d *rpm_mm) + (point_12_gap*rpm_mm))*1, profile_vel=500000, profile_acc=100000, profile_dec=100000)
    
        # 移动到12 孔托盘 10 点位(x)
        # x.pp_absolute_move(target_pos=point_12[0] - ((point_12_d *rpm_mm) + (point_12_gap*rpm_mm))*1, profile_vel=500000, profile_acc=100000, profile_dec=100000)
        # y.pp_absolute_move(target_pos=point_12[1], profile_vel=500000, profile_acc=100000, profile_dec=100000)
        
        # 移动到24 孔托盘 00 点位
        # x.pp_absolute_move(target_pos=point_24[0], profile_vel=500000, profile_acc=100000, profile_dec=100000)
        # y.pp_absolute_move(target_pos=point_24[1], profile_vel=500000, profile_acc=100000, profile_dec=100000)

        # 移动到24 孔托盘 01 点位
        # x.pp_absolute_move(target_pos=point_24[0], profile_vel=500000, profile_acc=100000, profile_dec=100000)
        # y.pp_absolute_move(target_pos=point_24[1] - ((point_24_d *rpm_mm)+ (point_24_gap*rpm_mm))*1, profile_vel=500000, profile_acc=100000, profile_dec=100000)
    
        # 移动到24 孔托盘 10 点位
        # x.pp_absolute_move(target_pos=point_24[0] - ((point_24_d *rpm_mm) + (point_24_gap*rpm_mm))*1, profile_vel=500000, profile_acc=100000, profile_dec=100000)
        # y.pp_absolute_move(target_pos=point_24[1], profile_vel=500000, profile_acc=100000, profile_dec=100000)
        

        
        # 移动到48 孔托盘 00 点位
        # x.pp_absolute_move(target_pos=point_48[0], profile_vel=500000, profile_acc=100000, profile_dec=100000)
        # y.pp_absolute_move(target_pos=point_48[1], profile_vel=500000, profile_acc=100000, profile_dec=100000)

        # 移动到48 孔托盘 01 点位
        # x.pp_absolute_move(target_pos=point_48[0], profile_vel=500000, profile_acc=100000, profile_dec=100000)
        # y.pp_absolute_move(target_pos=point_48[1] - ((point_48_d *rpm_mm)+ (point_48_gap*rpm_mm))*1, profile_vel=500000, profile_acc=100000, profile_dec=100000)
    
        # 移动到48 孔托盘 10 点位
        # x.pp_absolute_move(target_pos=point_48[0] - ((point_48_d *rpm_mm) + (point_48_gap*rpm_mm))*1, profile_vel=500000, profile_acc=100000, profile_dec=100000)
        # y.pp_absolute_move(target_pos=point_48[1], profile_vel=500000, profile_acc=100000, profile_dec=100000)
        

        # x 轴正向移动 3 毫米
        # x.pp_relative_move( 30 * rpm_mm, 50000,50000,50000)
        # x 轴逆向移动 3 毫米
        # x.pp_relative_move( -30 * rpm_mm, 50000,50000,50000)
        # y轴正向移动 3毫米
        # y.pp_relative_move(30 * rpm_mm,50000,50000,50000)
        # y轴逆向移动 3毫米
        # y.pp_relative_move(30 * rpm_mm,50000,50000,50000)

        # 其他模式调用-轮廓速度模式启动 运行2秒后停止
        # x.pv_start(target_velocity=6000, profile_acc=6000, profile_dec=6000)
        # time.sleep(10)
        # x.pv_stop()
        # 其他模式调用-速度模式(VM)启动与停止
        # x.vl_start(target_velocity=10, acceleration=10, acc_time=1, deceleration=10, dec_time=1)
        # time.sleep(2)
        # x.vl_stop()