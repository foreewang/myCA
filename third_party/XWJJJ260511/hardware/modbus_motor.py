"""基于 Modbus 的聚焦电机适配器。

本文件的作用很简单：
把你已有的 `modbus.py` 和 `MotorManager.py` 包装成项目统一的 `MotorBase` 接口。

自动对焦算法只认识三个方法：
1. move_to(position): 移动到某个焦距位置
2. get_position(): 读取当前位置
3. get_range(): 返回允许搜索的范围

因此其它代码不用关心底层是 Modbus、串口、步进电机还是虚拟电机。
"""

# 允许在类型标注里使用尚未定义完成的类名，减少运行时类型解析问题。
from __future__ import annotations

# Optional 表示参数可以是某个类型或 None；Tuple 用来标注固定长度的返回元组。
from typing import Optional, Tuple

# MotorBase 是项目统一的电机抽象接口，本类需要实现它规定的方法。
from .motor import MotorBase
# ModbusRTUClient 是底层 Modbus RTU 串口通信客户端。
from ..modbus import ModbusRTUClient
# MotorManager 封装了模式切换、使能、绝对运动等电机动作细节。
from ..MotorManager import MotorManager


class ModbusFocusMotor(MotorBase):
    """聚焦轴电机。

    这里默认使用绝对位置模式 PP：
    自动对焦算法给一个 position，本类就调用 MotorManager.pp_absolute_move()
    让真实焦距电机移动到这个绝对位置。
    """

    def __init__(
        self,
        *,
        # 电机串口号，Windows 下一般是 COM3、COM4 这种名字。
        port: str = "COM3",
        # 串口波特率，必须和电机驱动器设置一致。
        baudrate: int = 115200,
        # Modbus 从站号，也就是当前聚焦轴驱动器的站号。
        slave: int = 3,
        # 软件允许的最小位置，自动对焦不会主动跑到这个值以外。
        min_pos: float = -2100000,
        # 软件允许的最大位置，自动对焦不会主动跑到这个值以外。
        max_pos: float = -1900000,
        # PP 绝对位置模式下的运动速度，单位通常是脉冲/秒。
        profile_vel: int = 100000,
        # PP 绝对位置模式下的加速度。
        profile_acc: int = 100000,
        # PP 绝对位置模式下的减速度。
        profile_dec: int = 100000,
        # 单次移动最多等待多久，超过后认为运动失败。
        timeout: float = 1200.0,
        # 可选的外部 Modbus 客户端；如果传入，就复用外部连接。
        client: Optional[ModbusRTUClient] = None,
    ):
        """初始化聚焦电机。

        参数说明：
        - port: 串口号，例如 COM3。
        - baudrate: 波特率，需要和驱动器一致。
        - slave: Modbus 从站号。你现在的聚焦轴示例里是 slave=3。
        - min_pos/max_pos: 自动对焦允许搜索的安全位置范围。
        - profile_vel/profile_acc/profile_dec: PP 位置模式下的速度、加速度、减速度。
        - timeout: 单次移动等待到位的最长时间。
        - client: 可选。外部如果已经创建了 ModbusRTUClient，可以传进来复用。
        """

        # 把最小位置转成 float，后续搜索算法通常使用浮点位置。
        self._min = float(min_pos)
        # 把最大位置转成 float，和 _min 一起构成本对象的软件限位。
        self._max = float(max_pos)
        # 保存速度参数，真正移动时会传给 MotorManager。
        self._profile_vel = int(profile_vel)
        # 保存加速度参数，真正移动时会传给 MotorManager。
        self._profile_acc = int(profile_acc)
        # 保存减速度参数，真正移动时会传给 MotorManager。
        self._profile_dec = int(profile_dec)
        # 保存运动超时时间，避免电机一直等待不返回。
        self._timeout = float(timeout)

        # 如果 client 是外部传进来的，关闭连接的责任也留给外部。
        # 如果 client 是这里创建的，close() 时由这里负责断开。
        # True 表示本对象创建了客户端，close() 时应该由本对象关闭它。
        self._owns_client = client is None
        # 如果外面传了 client 就复用；否则按 port/baudrate 创建新的串口客户端。
        self._client = client or ModbusRTUClient(port=port, baudrate=baudrate)
        # 保存 Modbus 从站号，读写寄存器时都要带上这个 slave。
        self._slave = int(slave)
        # 创建电机动作管理器，后续模式切换和运动都委托给它。
        self._manager = MotorManager(self._client, slave=self._slave)

    def connect(self) -> None:
        """确保串口已经连接；失败时直接抛异常，让调用方知道对焦不能继续。"""

        # 如果还没连接，就调用底层 client.connect()；连接失败时抛出明确异常。
        if not self._client.is_connected() and not self._client.connect():
            raise RuntimeError("连接 Modbus 聚焦电机失败")

    def close(self) -> None:
        """关闭串口连接。

        只有本对象自己创建的 client 才会在这里关闭。
        外部传进来的 client 不在这里关闭，避免影响其它轴。
        """

        # 只有本对象自己创建的连接，才由本对象负责断开。
        if self._owns_client:
            self._client.disconnect()

    def __enter__(self) -> "ModbusFocusMotor":
        """支持 with ModbusFocusMotor(...) as motor: 的写法。"""

        # 进入 with 块时先确保串口连接成功。
        self.connect()
        # 返回 self，调用方就可以在 with 里面直接使用 motor.move_to()。
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """退出 with 时自动关闭串口。"""

        # 离开 with 块时释放本对象拥有的串口连接。
        self.close()

    def move_to(self, position: float) -> None:
        """移动聚焦轴到指定绝对位置。

        自动对焦算法会不断调用这个方法：
        position -> 电机移动 -> 相机采图 -> 算清晰度。
        """

        # 每次移动前都先确保串口已经连接。
        self.connect()

        # 做一次软件限位，防止算法给出超过安全范围的位置。
        # min(self._max, position) 先限制上界，再用 max(self._min, ...) 限制下界。
        # round 后转 int，因为驱动器位置寄存器使用整数脉冲。
        target = int(round(max(self._min, min(self._max, float(position)))))

        # 调用你已有的 MotorManager，走 PP 绝对位置移动。
        # 这个方法内部会检查模式、使能电机、写目标位置、等待到位。
        diff = self._manager.pp_absolute_move(
            # 目标绝对位置，已经经过软件限位保护。
            target_pos=target,
            # 轮廓速度，来自配置文件或默认值。
            profile_vel=self._profile_vel,
            # 轮廓加速度，来自配置文件或默认值。
            profile_acc=self._profile_acc,
            # 轮廓减速度，来自配置文件或默认值。
            profile_dec=self._profile_dec,
            # 运动超时时间，避免电机卡住时程序无限等待。
            timeout=self._timeout,
        )
        # diff 为 None 表示底层运动失败，例如未使能、通信失败或运动超时。
        if diff is None:
            raise RuntimeError(f"聚焦电机移动失败，目标位置: {target}")

    def get_position(self) -> float:
        """读取聚焦轴当前实际位置。"""

        # 读位置前也要确保串口已经连接。
        self.connect()
        # 从驱动器当前位置寄存器读取 32 位有符号位置值。
        pos = self._client._read_32bit(self._slave, ModbusRTUClient.REG_CURRENT_POS)
        # 读取失败时底层会返回 None，这里转成明确异常。
        if pos is None:
            raise RuntimeError("读取聚焦电机当前位置失败")
        # 对外统一返回 float，方便和自动对焦算法里的浮点位置一起使用。
        return float(pos)

    def get_range(self) -> Tuple[float, float]:
        """返回自动对焦搜索范围。"""

        # 返回的是 config.yaml 里给的安全搜索范围，不是电机真实机械全行程。
        return (self._min, self._max)
