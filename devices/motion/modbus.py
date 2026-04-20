# -*- coding: utf-8 -*-
"""
Modbus RTU 客户端模块，基于 pymodbus 库实现。
本模块封装了与支持 CiA 402 协议的伺服驱动器进行 Modbus RTU 通信的功能，
支持轮廓位置模式（PP）下的绝对运动和相对运动。

依赖库版本：
    pymodbus == 3.6.9
"""

import logging
import time
from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ModbusRTUClient:
    """
    Modbus RTU 串行通信客户端类。
    提供与伺服驱动器进行数据读写、状态控制、模式切换及位置运动控制的功能。
    遵循 CiA 402 驱动行规，使用标准控制字和状态字。
    """

    # ================== 寄存器地址定义 ==================
    REG_CONTROLWORD = 896       # 控制字 (0x380)
    REG_STATUSWORD = 897        # 状态字 (0x381)
    REG_CIA402_MODE = 177       # CiA 402 工作模式 (0xB1)
    REG_MODE_SELECT = 962       # 模式选择 (0x3C2)
    REG_MODE_DISPLAY = 963      # 模式显示 (0x3C3)

    # ================== 32 位寄存器地址（高16位地址） ==================
    REG_TARGET_POS = 999        # 目标位置高16位，低16位地址为 1000
    REG_CURRENT_POS = 968       # 当前位置高16位，低16位地址为 969
    REG_CMD_POS = 966           # 指令位置高16位，低16位地址为 967
    REG_PROFILE_VEL_HIGH = 1016 # 轮廓速度高16位，低16位地址为 1017
    REG_PROFILE_ACC_HIGH = 1020 # 轮廓加速度高16位，低16位地址为 1021
    REG_PROFILE_DEC_HIGH = 1022 # 轮廓减速度高16位，低16位地址为 1023

    # ================== 状态字 (Statusword) 位掩码 ==================
    STAT_READY_TO_SWITCH_ON = 0x0001   # 准备接通
    STAT_SWITCHED_ON = 0x0002          # 已接通
    STAT_OPERATION_ENABLED = 0x0004    # 运行使能
    STAT_FAULT = 0x0008                # 故障
    STAT_VOLTAGE_ENABLED = 0x0010      # 主回路电压有效
    STAT_QUICK_STOP = 0x0020           # 快速停止
    STAT_SWITCH_ON_DISABLED = 0x0040   # 禁止接通
    STAT_WARNING = 0x0080              # 警告
    STAT_TARGET_REACHED = 0x0400       # 目标到达
    STAT_SETPOINT_ACK = 0x2000         # 设定值应答

    # ================== 控制字 (Controlword) 命令 ==================
    CMD_SHUTDOWN = 0x06                # 停机
    CMD_SWITCH_ON = 0x07               # 接通
    CMD_ENABLE_OPERATION = 0x0F        # 使能运行
    CMD_DISABLE_VOLTAGE = 0x00         # 禁止电压
    CMD_QUICK_STOP = 0x02              # 快速停止
    CMD_FAULT_RESET = 0x80             # 故障复位

    # ================== 轮廓速度模式 ==================
    REG_TARGET_VEL_HIGH = 0x448       # 目标速度高16位地址 (0x1C0)

    # 速度模式寄存器地址
    REG_VM_sTART= 0x7F                  # 速度模式启动
    REG_TARGET_VEL_382_HIGH = 0x382     # 目标速度高16位
    REG_ACC_389_HIGH = 0x389            # 加速度高16位
    REG_ACC_TIME_38B = 0x38B            # 加速度时间（16位）
    REG_DEC_38C_HIGH = 0x38C            # 减速度高16位
    REG_DEC_TIME_38E = 0x38E            # 减速度时间（16位）

    def __init__(self, port="COM3", baudrate=115200, bytesize=8, parity="N", stopbits=1, timeout=1):
        """
        初始化 Modbus RTU 客户端。

        :param port:     串口号，例如 "COM3" 或 "/dev/ttyUSB0"
        :param baudrate: 波特率，常见值 9600, 19200, 115200 等
        :param bytesize: 数据位，通常为 8
        :param parity:   校验位，"N" 无校验，"E" 偶校验，"O" 奇校验
        :param stopbits: 停止位，1 或 2
        :param timeout:  串口通信超时时间（秒）
        """
        self.port = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.timeout = timeout
        self._client = None
        self._connected = False

    def connect(self) -> bool:
        """建立与串口的连接。"""
        if self._connected:
            logger.info("已经连接，无需重复连接")
            return True
        self._client = ModbusSerialClient(
            method="rtu", port=self.port, baudrate=self.baudrate,
            bytesize=self.bytesize, parity=self.parity, stopbits=self.stopbits,
            timeout=self.timeout,
        )
        try:
            self._connected = self._client.connect()
            if self._connected:
                logger.info(f"成功连接到 {self.port} @ {self.baudrate}bps")
            else:
                logger.error(f"连接失败，请检查串口 {self.port}")
            return self._connected
        except Exception as e:
            logger.error(f"连接时发生异常: {e}")
            self._connected = False
            return False

    def disconnect(self):
        """关闭串口连接。"""
        if self._client and self._connected:
            self._client.close()
            self._connected = False
            logger.info("已断开连接")

    def is_connected(self) -> bool:
        """检查当前是否已连接。"""
        return self._connected and self._client is not None

    def read_holding_registers(self, slave: int, address: int, count: int = 1) -> list | None:
        """读取保持寄存器（功能码 0x03）。"""
        if not self.is_connected():
            logger.error("未连接，请先调用 connect()")
            return None
        try:
            result = self._client.read_holding_registers(address=address, count=count, slave=slave)
            if result.isError():
                logger.error(f"读保持寄存器失败: {result}")
                return None
            logger.info(f"读取保持寄存器成功: slave={slave}, address={address}, count={count}, values={result.registers}")
            return result.registers
        except ModbusException as e:
            logger.error(f"Modbus 异常: {e}")
            return None

    def _read_statusword(self, slave: int) -> int | None:
        """读取状态字（内部辅助方法）。"""
        regs = self.read_holding_registers(slave, self.REG_STATUSWORD, 1)
        return regs[0] if regs else None

    def write_register(self, slave: int, address: int, value: int) -> bool:
        """写单个保持寄存器（功能码 0x06）。"""
        if not self.is_connected():
            logger.error("未连接，请先调用 connect()")
            return False
        try:
            result = self._client.write_register(address=address, value=value, slave=slave)
            if result.isError():
                logger.error(f"写寄存器失败: {result}")
                return False
            logger.info(f"写寄存器成功: slave={slave}, address={address}, value={value}")
            return True
        except ModbusException as e:
            logger.error(f"Modbus 异常: {e}")
            return False

    def _write_controlword(self, slave: int, value: int) -> bool:
        """写控制字（内部辅助方法）。"""
        return self.write_register(slave, self.REG_CONTROLWORD, value)

    # ---------- 32 位寄存器读写 ----------
    def _write_32bit(self, slave: int, reg_high: int, value: int) -> bool:
        """将 32 位有符号整数写入两个连续的保持寄存器。"""
        if value < 0:
            value = value + (1 << 32)
        high = (value >> 16) & 0xFFFF
        low = value & 0xFFFF
        if not self.write_register(slave, reg_high, high):
            return False
        if not self.write_register(slave, reg_high + 1, low):
            return False
        return True

    def _read_32bit(self, slave: int, reg_high: int) -> int | None:
        """从两个连续的保持寄存器读取 32 位有符号整数。"""
        regs = self.read_holding_registers(slave, reg_high, 2)
        if regs is None or len(regs) < 2:
            return None
        high, low = regs[0], regs[1]
        val = (high << 16) | low
        if val & (1 << 31):
            val = val - (1 << 32)
        return val

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()

    # -------------------- 电机通用控制逻辑 -------------------------------
    def _wait_for_status_mask(self, slave: int, mask: int, expected: int, timeout: float = 10.0) -> bool:
        """
        等待状态字的指定掩码位达到期望值。
        """
        start = time.time()
        while time.time() - start < timeout:
            status = self._read_statusword(slave)
            logger.info(status & mask)
            logger.info(expected)
            logger.info((status & mask) == expected)
            if status is None:
                time.sleep(0.02)
                continue
            if (status & mask) == expected:
                return True
            time.sleep(0.02)
        final_status = self._read_statusword(slave)
        logger.error(f"等待状态掩码超时 (timeout={timeout}s): "+
                     f"mask=0x{mask:04X}, expected=0x{expected:04X} ")
        return False

    def fault_reset(self, slave: int) -> bool:
        """执行故障复位操作。"""
        if not self._write_controlword(slave, self.CMD_FAULT_RESET):
            return False
        time.sleep(0.05)
        self._write_controlword(slave, 0x00)
        status = self._read_statusword(slave)
        if status is None or (status & self.STAT_FAULT):
            logger.error("故障复位失败，故障位仍然存在")
            return False
        logger.info("故障复位成功")
        return True

    def enable_motor(self, slave: int) -> bool:
        """
        使能电机，按照 CiA 402 标准状态机执行 Shutdown → Switch on → Enable operation 序列。
        """
        status = self._read_statusword(slave)
        # if status is not None and (status & self.STAT_OPERATION_ENABLED):
        #     logger.info("电机已处于使能状态")
        #     return True

        # 如果存在故障，先复位
        if status is not None and (status & self.STAT_FAULT):
            logger.warning("检测到故障，尝试复位")
            if not self.fault_reset(slave):
                logger.error("故障复位失败")
                return False
            status = self._read_statusword(slave)

        # 确保进入 Switch on disabled 状态
        if status is not None and not (status & self.STAT_SWITCH_ON_DISABLED):
            logger.info("发送 Disable voltage 进入 Switch on disabled")
            if not self._write_controlword(slave, self.CMD_DISABLE_VOLTAGE):
                return False
            time.sleep(0.1)
            if not self._wait_for_status_mask(slave, 0x004F, self.STAT_SWITCH_ON_DISABLED, timeout=1.0):
                logger.warning("未进入预期的 Switch on disabled，继续尝试使能")

        # 标准使能序列
        if not self._write_controlword(slave, self.CMD_SHUTDOWN):
            return False
        if not self._wait_for_status_mask(slave, 0x0007, self.STAT_READY_TO_SWITCH_ON):
            return False

        if not self._write_controlword(slave, self.CMD_SWITCH_ON):
            return False
        if not self._wait_for_status_mask(slave, 0x0007, self.STAT_READY_TO_SWITCH_ON | self.STAT_SWITCHED_ON):
            return False

        if not self._write_controlword(slave, self.CMD_ENABLE_OPERATION):
            return False
        if not self._wait_for_status_mask(slave, self.STAT_OPERATION_ENABLED, 4):
            return False

        logger.info("电机使能成功")
        return True

    def switch_mode(self, slave: int, mode_value: int, auto_enable: bool = True) -> bool:
        """
        切换驱动器工作模式（如轮廓位置模式 PP、轮廓速度模式 PV 等）。
        """
        status = self._read_statusword(slave)
        if status is None:
            logger.error("无法读取状态字")
            return False

        if status & self.STAT_FAULT:
            logger.warning("检测到故障，正在自动复位...")
            if not self.fault_reset(slave):
                logger.error("故障复位失败，无法切换模式")
                return False
            status = self._read_statusword(slave)

        # 如果电机已使能，先禁止电压，进入 Switch on disabled
        if status & self.STAT_OPERATION_ENABLED:
            logger.info("当前电机处于使能状态，先执行去使能...")
            if not self._write_controlword(slave, self.CMD_DISABLE_VOLTAGE):
                logger.error("发送 Disable voltage 失败")
                return False
            # 等待进入 Switch on disabled 状态
            if not self._wait_for_status_mask(slave, 0x004F, self.STAT_SWITCH_ON_DISABLED, timeout=1.0):
                logger.warning("去使能后未进入预期的 Switch on disabled")
            time.sleep(0.05)

        # 写入模式
        if not self.write_register(slave, self.REG_MODE_SELECT, mode_value):
            logger.error(f"写入模式 {hex(mode_value)} 失败")
            return False
        # 验证模式
        mode_display = self.read_holding_registers(slave, self.REG_MODE_DISPLAY, 1)
        if mode_display and mode_display[0] != mode_value:
            logger.warning(f"模式切换后读取 6061h 为 {hex(mode_display[0])}，与预期 {hex(mode_value)} 不符")
        logger.info(f"成功切换到模式 {hex(mode_value)}")

        if auto_enable:
            return self.enable_motor(slave)
        return True

    def _restore_enabled_state(self, slave: int, timeout: float = 1.0) -> bool:
        """运动结束后恢复使能状态。"""
        if not self._write_controlword(slave, self.CMD_ENABLE_OPERATION):
            logger.error("恢复使能状态失败：写入控制字失败")
            return False
        start = time.time()
        while time.time() - start < timeout:
            status = self._read_statusword(slave)
            if status is not None and (status & self.STAT_OPERATION_ENABLED):
                logger.info("电机已恢复到使能状态")
                return True
            time.sleep(0.02)
        logger.error("恢复使能状态超时：未检测到 OPERATION_ENABLED 位")
        return False

    # ------------------ 轮廓位置模式控制逻辑 ---------------------------
    def move_absolute_pp(self, slave: int, target_pos: int,
                         profile_vel: int, profile_acc: int, profile_dec: int,
                         timeout: float = 1200.0) -> int | None:
        """轮廓位置模式（PP）下的绝对位置运动。"""
        status = self._read_statusword(slave)
        if status is None:
            logger.error("无法读取状态字")
            return None
        if not (status & self.STAT_OPERATION_ENABLED):
            logger.error("电机未使能，请先调用 enable_motor() 并确保处于 PP 模式")
            return None

        # 写入轮廓参数
        if not self._write_32bit(slave, self.REG_PROFILE_VEL_HIGH, profile_vel):
            logger.error("设置轮廓速度失败")
            return None
        if not self._write_32bit(slave, self.REG_PROFILE_ACC_HIGH, profile_acc):
            logger.error("设置轮廓加速度失败")
            return None
        if not self._write_32bit(slave, self.REG_PROFILE_DEC_HIGH, profile_dec):
            logger.error("设置轮廓减速度失败")
            return None

        # 写入目标位置
        if not self._write_32bit(slave, self.REG_TARGET_POS, target_pos):
            logger.error("设置目标位置失败")
            return None
        time.sleep(0.02)

        # 触发运动：控制字 = ENABLE_OPERATION + 新位置位 (0x10)
        trigger_cmd = self.CMD_ENABLE_OPERATION | 0x10  # 0x1F
        if not self._write_controlword(slave, trigger_cmd):
            logger.error("触发运动失败")
            return None

        # 等待当前位置等于目标位置
        start_time = time.time()
        current_pos = None
        while time.time() - start_time < timeout:
            current_pos = self._read_32bit(slave, self.REG_CURRENT_POS)
            if current_pos is None:
                time.sleep(0.05)
                continue
            if abs(current_pos - target_pos) <= 50:
                logger.info(f"目标位置已到达: {current_pos}")
                break
            status = self._read_statusword(slave)
            if status is not None and (status & self.STAT_FAULT):
                logger.error("运动过程中发生故障")
                return None
            time.sleep(0.05)
        else:
            logger.error(f"运动超时（{timeout}秒），当前位置 {current_pos} != 目标 {target_pos}")
            self._restore_enabled_state(slave)
            return None

        # 运动正常结束，恢复使能
        if not self._restore_enabled_state(slave):
            logger.warning("恢复使能状态失败，但运动已完成")
        else:
            logger.info("运动完成，电机已恢复使能")
        self.quick_stop(slave=slave)
        logger.info("运动完成，电机停止")
        # 计算位置误差
        final_pos = self._read_32bit(slave, self.REG_CURRENT_POS)
        cmd_pos = self._read_32bit(slave, self.REG_CMD_POS)
        if final_pos is None or cmd_pos is None:
            logger.error("读取最终位置或指令位置失败")
            return None

        diff = final_pos - cmd_pos
        logger.info(f"运动完成: 目标={target_pos}, 当前位置={final_pos}, 指令位置={cmd_pos}, 差值={diff}")
        return diff

    def move_relative_pp(self, slave: int, offset: int,
                         profile_vel: int, profile_acc: int, profile_dec: int,
                         timeout: float = 1200.0) -> int | None:
        """轮廓位置模式（PP）下的相对位置运动。"""
        status = self._read_statusword(slave)
        if status is None:
            logger.error("无法读取状态字")
            return None
        if not (status & self.STAT_OPERATION_ENABLED):
            logger.error("电机未使能，请先调用 enable_motor() 并确保处于 PP 模式")
            return None

        start_pos = self._read_32bit(slave, self.REG_CURRENT_POS)
        if start_pos is None:
            logger.error("读取起始位置失败")
            return None

        target_abs_pos = start_pos + offset

        # 写入轮廓参数
        if not self._write_32bit(slave, self.REG_PROFILE_VEL_HIGH, profile_vel):
            logger.error("设置轮廓速度失败")
            return None
        if not self._write_32bit(slave, self.REG_PROFILE_ACC_HIGH, profile_acc):
            logger.error("设置轮廓加速度失败")
            return None
        if not self._write_32bit(slave, self.REG_PROFILE_DEC_HIGH, profile_dec):
            logger.error("设置轮廓减速度失败")
            return None

        # 写入相对偏移量
        if not self._write_32bit(slave, self.REG_TARGET_POS, offset):
            logger.error("设置相对偏移量失败")
            return None
        time.sleep(0.02)

        # 触发相对运动：控制字 = ENABLE_OPERATION + 相对位 (0x40) + 新位置位 (0x10) = 0x5F
        trigger_cmd = self.CMD_ENABLE_OPERATION | 0x40 | 0x10
        if not self._write_controlword(slave, trigger_cmd):
            logger.error("触发相对运动失败")
            return None

        # 等待当前位置等于绝对目标位置
        start_time = time.time()
        current_pos = start_pos
        
        while time.time() - start_time < timeout:
            current_pos = self._read_32bit(slave, self.REG_CURRENT_POS)
            if current_pos is None:
                time.sleep(0.05)
                continue
            if abs(current_pos - target_abs_pos) <= 50:
                logger.info(f"相对位置运动完成: {current_pos}")
                break
            status = self._read_statusword(slave)
            if status is not None and (status & self.STAT_FAULT):
                logger.error("运动过程中发生故障")
                return None
            time.sleep(0.05)
        else:
            logger.error(f"运动超时（{timeout}秒），当前位置 {current_pos} != 目标 {target_abs_pos}")
            self._restore_enabled_state(slave)
            return None

        if not self._restore_enabled_state(slave):
            logger.warning("恢复使能状态失败，但运动已完成")
        else:
            logger.info("相对运动完成，电机已恢复使能")
        self.quick_stop(slave=slave)
        logger.info("运动完成，电机停止")
        final_pos = self._read_32bit(slave, self.REG_CURRENT_POS)
        cmd_pos = self._read_32bit(slave, self.REG_CMD_POS)
        if final_pos is None or cmd_pos is None:
            logger.error("读取最终位置或指令位置失败")
            return None

        diff = final_pos - cmd_pos
        logger.info(f"相对运动完成: 起始={start_pos}, 偏移={offset}, "
                    f"期望绝对位置={target_abs_pos}, 当前位置={final_pos}, 指令位置={cmd_pos}, 差值={diff}")
        return diff

    # ----------------------- 轮廓速度模式控制逻辑 --------------------------
    def start_velocity_mode(self, slave: int, target_velocity: int,
                            profile_acc: int, profile_dec: int) -> bool:
        """启动轮廓速度模式（PV）运行。"""
        if not self._write_32bit(slave, self.REG_TARGET_VEL_HIGH, target_velocity):
            logger.error("写入目标速度失败")
            return False
        if not self._write_32bit(slave, self.REG_PROFILE_ACC_HIGH, profile_acc):
            logger.error("写入轮廓加速度失败")
            return False
        if not self._write_32bit(slave, self.REG_PROFILE_DEC_HIGH, profile_dec):
            logger.error("写入轮廓减速度失败")
            return False

        if not self.enable_motor(slave):
            logger.error("电机使能失败，无法启动速度模式")
            return False

        logger.info(f"速度模式已启动: 目标速度={target_velocity} 脉冲/秒")
        return True

    def stop_velocity(self, slave: int) -> bool:
        """停止轮廓速度模式运行。"""
        if not self._write_32bit(slave, self.REG_TARGET_VEL_HIGH, 0):
            logger.error("写入目标速度0失败")
            return False
        logger.info("速度模式停止命令已发送，电机正在减速")
        return True

    # ------------------------- 速度模式电机控制逻辑 ----------------------
    def start_speed_mode(self, slave: int, target_velocity: int,
                         acceleration: int, acc_time: int,
                         deceleration: int, dec_time: int) -> bool:
        """启动速度模式运行。"""
        if not self.write_register(slave, self.REG_TARGET_VEL_382_HIGH, target_velocity):
            logger.error("写入目标速度失败")
            return False
        if not self._write_32bit(slave, self.REG_ACC_389_HIGH, acceleration):
            logger.error("写入加速度失败")
            return False
        if not self.write_register(slave, self.REG_ACC_TIME_38B, acc_time):
            logger.error("写入加速度时间失败")
            return False
        if not self._write_32bit(slave, self.REG_DEC_38C_HIGH, deceleration):
            logger.error("写入减速度失败")
            return False
        if not self.write_register(slave, self.REG_DEC_TIME_38E, dec_time):
            logger.error("写入减速度时间失败")
            return False
        
        if not self._write_controlword(slave, self.REG_VM_sTART):
            return False
        logger.info(f"速度模式已启动: 目标速度={target_velocity}, 加速度={acceleration}, 减速度={deceleration}")
        return True


    # ------------------------- 快速停止方法 ----------------------------
    def quick_stop(self, slave: int) -> bool:
        """
        发送快速停止命令（控制字 0x02）。
        电机将按当前设定的减速度减速停止，状态变为 Quick stop active。
        """
        if not self._write_controlword(slave, self.CMD_SHUTDOWN):
            return False
        time.sleep(0.05)
        logger.info(f"从站 {slave} 快速停止命令已发送")
        return True