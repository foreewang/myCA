"""给其它 Python 程序调用的自动对焦接口。

你要交给别人调用时，最重要的就是这个文件里的函数：

    run_realtime_autofocus(...)

它会完成一整套流程：
1. 移动聚焦电机到某个位置
2. 等电机稳定
3. 相机采一帧图
4. 计算清晰度
5. 用搜索算法继续找更清晰的位置
6. 最后把电机停在最佳焦距位置，并返回结果
"""

# 允许类型标注引用稍后才定义的类，减少导入时的类型解析成本。
from __future__ import annotations

# time 用来等待电机稳定、统计自动对焦耗时。
import time
# dataclass 用来定义结果对象，比普通 dict 更清楚。
from dataclasses import dataclass
from collections.abc import Mapping
# List/Optional/Tuple 用于类型标注，方便看懂参数和返回值。
from typing import Any, List, Optional, Tuple

# OpenCV 用来保存最终最清晰图像。
import cv2
# numpy 用来标注图像帧类型。
import numpy as np

# 自动对焦搜索函数和清晰度评价函数。
from .focus import auto_focus, compute_focus_metric
# MotorBase 是统一电机接口；VirtualMotor 是不控制真实硬件的虚拟电机。
from .hardware import MotorBase, VirtualMotor
# HikrobotCamera 是统一相机接口，内部支持 MVS 或 OpenCV。
from .hardware.hikrobot_camera import HikrobotCamera


def _resolve_focus_range(
    objective: Optional[Any], focus_ranges: Mapping[Any, Mapping[str, Any]]
) -> Tuple[float, float]:
    """根据倍镜选择自动对焦范围。"""

    if objective is None:
        raise ValueError("传入 focus_ranges 时必须同时传 objective，例如 objective='4x'")

    target = _normalize_objective_key(objective)
    available: list[str] = []
    for key, value in focus_ranges.items():
        available.append(str(key))
        if _normalize_objective_key(key) == target:
            if "min_pos" not in value or "max_pos" not in value:
                raise ValueError(f"focus_ranges[{key!r}] 必须包含 min_pos 和 max_pos")
            min_pos = float(value["min_pos"])
            max_pos = float(value["max_pos"])
            if min_pos >= max_pos:
                raise ValueError(f"focus_ranges[{key!r}] 的 min_pos 必须小于 max_pos")
            return min_pos, max_pos

    choices = "、".join(available) if available else "空"
    raise ValueError(f"未知倍镜 {objective!r}，可选倍镜：{choices}")


def _normalize_objective_key(value: Any) -> str:
    text = str(value).strip().lower()
    for suffix in ("倍镜", "倍", "x", "镜"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    chinese_numbers = {
        "一": "1",
        "二": "2",
        "三": "3",
        "四": "4",
        "五": "5",
        "六": "6",
        "七": "7",
        "八": "8",
        "九": "9",
        "十": "10",
    }
    if text in chinese_numbers:
        return chinese_numbers[text]
    try:
        number = float(text)
    except ValueError:
        return text
    if number.is_integer():
        return str(int(number))
    return str(number)


@dataclass
class AutoFocusResult:
    """一次自动对焦完成后的返回结果。"""

    best_pos: float
    """最佳焦距位置，也就是最终电机会移动到的位置。"""

    best_value: float
    """最佳位置对应的清晰度分数，数值越大越清晰。"""

    frame: np.ndarray
    """最佳焦距位置处重新采集的一帧图像，BGR 格式。"""

    focus_log: List[Tuple[float, float]]
    """搜索过程记录，每一项是 (位置, 清晰度分数)。"""

    output_path: Optional[str]
    """如果保存了最清晰图片，这里是图片路径；不保存时为 None。"""

    elapsed_sec: float
    """本次自动对焦耗时，单位秒。"""


def run_realtime_autofocus(
    *,
    # 外部已经创建好的电机对象；为空时函数内部自动创建。
    motor: Optional[MotorBase] = None,
    # 外部已经创建好的相机对象；为空时函数内部自动创建。
    camera: Optional[HikrobotCamera] = None,
    # True 表示用真实 Modbus 电机；False 表示用虚拟电机。
    use_modbus_motor: bool = True,
    # 真实电机串口号。
    motor_port: str = "COM3",
    # 真实电机串口波特率。
    motor_baudrate: int = 115200,
    # 聚焦轴 Modbus 从站号。
    focus_slave: int = 3,
    # 自动对焦搜索最小位置，也是软件限位。
    min_pos: float = -2100000,
    # 自动对焦搜索最大位置，也是软件限位。
    max_pos: float = -1900000,
    # 当前倍镜，例如 4x、10x。传了 focus_ranges 时，会按这个值自动选择搜索范围。
    objective: Optional[Any] = None,
    # 倍镜到搜索范围的映射，例如 {"4x": {"min_pos": ..., "max_pos": ...}}。
    focus_ranges: Optional[Mapping[Any, Mapping[str, Any]]] = None,
    # 电机 PP 位置模式速度。
    profile_vel: int = 100000,
    # 电机 PP 位置模式加速度。
    profile_acc: int = 100000,
    # 电机 PP 位置模式减速度。
    profile_dec: int = 100000,
    # 搜索位置收敛精度。
    tol: float = 5,
    # 最大搜索迭代次数。
    max_iter: int = 30,
    # 每次电机移动后等待画面稳定的毫秒数。
    settle_ms: float = 200,
    # 清晰度计算时使用的中心 ROI 比例。
    center_roi: float = 0.6,
    # 清晰度计算前的下采样比例。
    downsample: float = 0.5,
    # 最清晰图片保存路径；None 表示不保存。
    output_path: Optional[str] = "sharpest.png",
    # True 表示自动创建相机时使用 MVS。
    use_mvs: bool = True,
    # OpenCV 相机索引，仅 use_mvs=False 时使用。
    camera_index: int = 0,
    # MVS 网口相机 IP。
    camera_ip: Optional[str] = None,
    # 连接相机的电脑网卡 IP。
    camera_net_export_ip: Optional[str] = None,
    # MVS Python SDK 的 MvImport 目录。
    mvs_sdk_path: Optional[str] = None,
    # 是否启用相机自动曝光；None 表示不改相机当前设置。
    camera_exposure_auto: Optional[bool] = None,
    # 手动曝光时间，单位微秒；None 表示不改相机当前设置。
    camera_exposure_time_us: Optional[float] = None,
) -> AutoFocusResult:
    """执行一次实时自动对焦。

    典型调用方式：

        result = run_realtime_autofocus(
            use_modbus_motor=True,
            motor_port="COM3",
            focus_slave=3,
            objective="4x",
            focus_ranges={
                "4x": {"min_pos": -2063120, "max_pos": -1769500},
                "10x": {"min_pos": -2095551, "max_pos": -2028750},
            },
        )

    参数说明：
    - motor: 可选。外部已经有电机对象时传入；不传则本函数自动创建。
    - camera: 可选。外部已经有相机对象时传入；不传则本函数自动创建。
    - use_modbus_motor: True 表示使用真实 Modbus 聚焦电机；False 表示使用虚拟电机。
    - motor_port/motor_baudrate/focus_slave: 真实 Modbus 电机连接参数。
    - min_pos/max_pos: 自动对焦搜索范围，也是软件限位。
    - objective/focus_ranges: 调用方只传当前倍镜时，用这组映射自动选择 min_pos/max_pos。
    - profile_vel/profile_acc/profile_dec: 电机 PP 位置模式运动参数。
    - tol: 搜索收敛精度，越小越精细，但耗时更长。
    - max_iter: 最大搜索次数。
    - settle_ms: 电机移动后等待稳定的时间。
    - center_roi: 只计算图像中心区域的清晰度，0 表示全图。
    - downsample: 算清晰度前的下采样比例，用来降低噪点影响。
    - output_path: 最清晰图片保存路径；传 None 则不保存。
    - use_mvs/camera_index: 自动创建相机时使用。MVS 可按 camera_ip 打开网口相机。
    - camera_exposure_auto/camera_exposure_time_us: 自动创建相机时的曝光配置。
    """

    if focus_ranges is not None:
        min_pos, max_pos = _resolve_focus_range(objective, focus_ranges)

    # 记录相机和电机是不是本函数创建的。
    # 如果是本函数创建的，结束时本函数负责关闭。
    # 如果是外部传进来的，结束时不主动关闭，避免影响外部程序继续使用。
    owns_camera = camera is None
    owns_motor = motor is None

    # 如果调用方没有传相机对象，就按参数创建一个相机对象。
    if camera is None:
        camera = HikrobotCamera(
            device=camera_ip,
            use_mvs=use_mvs,
            opencv_index=camera_index,
            net_export_ip=camera_net_export_ip,
            mvs_sdk_path=mvs_sdk_path,
            exposure_auto=camera_exposure_auto,
            exposure_time_us=camera_exposure_time_us,
        )

    # 如果调用方没有传电机对象，就按参数创建真实电机或虚拟电机。
    if motor is None:
        if use_modbus_motor:
            # 真实电机模式才导入 ModbusFocusMotor。
            # 这样没有安装 pymodbus 时，虚拟测试模式仍然可以导入本文件。
            from .hardware.modbus_motor import ModbusFocusMotor

            motor = ModbusFocusMotor(
                port=motor_port,
                baudrate=motor_baudrate,
                slave=focus_slave,
                min_pos=min_pos,
                max_pos=max_pos,
                profile_vel=profile_vel,
                profile_acc=profile_acc,
                profile_dec=profile_dec,
            )
        else:
            # 虚拟电机只记录位置，不会写串口和寄存器，适合离线测试算法。
            motor = VirtualMotor(min_pos=min_pos, max_pos=max_pos)

    # 从电机对象拿到它允许自动对焦搜索的范围。
    motor_min, motor_max = motor.get_range()
    # 把毫秒转换成秒，time.sleep 使用秒作为单位。
    settle_sec = settle_ms / 1000.0
    # 保存每一次搜索采样的位置和清晰度，最后写成 focus_log.csv。
    focus_log: List[Tuple[float, float]] = []
    # center_roi <= 0 表示全图；否则限制到最大 1.0。
    roi = None if center_roi <= 0 else min(1.0, center_roi)

    def move_and_capture(position: float) -> float:
        """搜索算法每试一个位置，就会调用一次这个函数。"""

        # 1. 电机移动到指定位置。
        motor.move_to(position)

        # 2. 等机械和画面稳定。
        time.sleep(settle_sec)

        # 3. 相机采一帧。
        frame = camera.capture()

        # 4. 算清晰度。这里用 focus.py 里的 Laplacian 方差。
        value = compute_focus_metric(frame, center_roi=roi, downsample=downsample)

        # 5. 记录过程，方便后面查看每个位置的分数。
        focus_log.append((float(position), value))
        return value

    try:
        # 记录开始时间，用于计算自动对焦耗时。
        t0 = time.perf_counter()

        # auto_focus 会在 [motor_min, motor_max] 内搜索清晰度最高的位置。
        best_pos, best_value = auto_focus(
            move_and_capture,
            motor_min=motor_min,
            motor_max=motor_max,
            tol=tol,
            max_iter=max_iter,
        )
        # 搜索算法结束后计算耗时。
        elapsed = time.perf_counter() - t0

        # 搜索结束后，再把电机移动到最佳位置，并重新采一帧作为最终结果图。
        motor.move_to(best_pos)
        time.sleep(settle_sec)
        frame = camera.capture()

        # 如果指定了输出路径，就保存最终最佳位置处重新采集的一帧。
        if output_path:
            cv2.imwrite(output_path, frame)

        # 日志按位置排序，方便查看焦点曲线。
        focus_log.sort(key=lambda item: item[0])
        # 把本次自动对焦的核心结果打包返回。
        return AutoFocusResult(
            best_pos=best_pos,
            best_value=best_value,
            frame=frame,
            focus_log=focus_log,
            output_path=output_path,
            elapsed_sec=elapsed,
        )
    finally:
        # 本函数创建的相机，由本函数关闭。
        if owns_camera:
            camera.close()
        # 本函数创建的电机，如果有 close 方法，也由本函数关闭。
        if owns_motor and hasattr(motor, "close"):
            motor.close()
