"""
作为 workflow 层对底层相机控制器的二次封装，为上层扫描流程提供更稳定、统一的拍照接口
2026/4/21
修复了前一版“set_exposure_time 调错接口并被静默吞掉”的问题，（未测试）
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

# 约定项目根目录为当前脚本所在目录的上两级：
# project_root/
# ├─ devices/
# ├─ workflow/
# └─ ...
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEVICES_DIR = PROJECT_ROOT / "devices"

# 为兼容“直接运行脚本”而非 package 启动的场景，
# 将 devices 目录加入导入路径，确保可以导入底层相机控制器。
if str(DEVICES_DIR) not in sys.path:
    sys.path.insert(0, str(DEVICES_DIR))

from camera_controller import HikCameraController  # type: ignore


def build_image_name(pattern: str, format_kwargs: Dict[str, Any]) -> str:
    """
    根据命名模板生成图像文件名。

    参数
    ----
    pattern : str
        文件名模板，例如：
        "{task_id}_{well}_{index:02d}.bmp"
    format_kwargs : Dict[str, Any]
        模板所需格式化字段。

    返回
    ----
    str
        生成后的文件名。

    异常
    ----
    KeyError
        当模板缺少所需字段时抛出，并给出当前可用字段列表，
        方便快速定位命名模板配置错误。

    说明
    ----
    将命名逻辑独立出来，便于：
    1. 统一文件命名规则；
    2. 单独测试模板合法性；
    3. 后续修改命名策略时只改这一处。
    """
    try:
        return pattern.format(**format_kwargs)
    except KeyError as exc:
        raise KeyError(
            f"文件名模板缺少字段 {exc}。当前可用字段: {sorted(format_kwargs.keys())}"
        ) from exc


def _safe_int_attr(obj: Any, name: str, default: int | None = None) -> int | None:
    """
    安全读取对象属性并尝试转换为 int。

    参数
    ----
    obj : Any
        待读取属性的对象。
    name : str
        属性名。
    default : int | None, optional
        属性不存在或转换失败时返回的默认值。

    返回
    ----
    int | None
        成功时返回整数，失败时返回 default。

    说明
    ----
    相机 SDK 返回的帧对象在不同包装版本下，字段名和字段类型可能不完全一致。
    这里做一层宽松兼容，减少上层代码对底层实现细节的依赖。
    """
    value = getattr(obj, name, None)
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def frameinfo_to_dict(frame: Any, saved_path: str) -> Dict[str, Any]:
    """
    将相机返回的帧信息对象转换为统一的可序列化字典。

    参数
    ----
    frame : Any
        相机 SDK 返回的帧对象。允许为 None。
    saved_path : str
        图像保存路径。

    返回
    ----
    Dict[str, Any]
        统一格式的帧信息字典，便于写入 JSON、日志或上层结果对象。

    说明
    ----
    这里同时兼容两类字段命名：
    - Python 风格字段：width / height / frame_num ...
    - SDK 原始字段：nWidth / nHeight / nFrameNum ...

    这样无论底层返回 dataclass、普通对象还是 ctypes 包装对象，
    上层都可以拿到同一套结果结构。
    """
    if frame is None:
        return {
            "saved_path": saved_path,
            "width": None,
            "height": None,
            "frame_num": None,
            "pixel_type": None,
            "frame_len": None,
        }

    return {
        "saved_path": saved_path,
        "width": _safe_int_attr(frame, "width", _safe_int_attr(frame, "nWidth")),
        "height": _safe_int_attr(frame, "height", _safe_int_attr(frame, "nHeight")),
        "frame_num": _safe_int_attr(frame, "frame_num", _safe_int_attr(frame, "nFrameNum")),
        "pixel_type": _safe_int_attr(frame, "pixel_type", _safe_int_attr(frame, "enPixelType")),
        "frame_len": _safe_int_attr(frame, "frame_len", _safe_int_attr(frame, "nFrameLen")),
    }


def open_camera(
    *,
    mvs_python_dir: str | None = None,
    device_index: int = 0,
    serial_number: str | None = None,
    exposure_us: int | float | None = None,
    gain: float | None = None,
) -> HikCameraController:
    """
    打开相机，并按需设置曝光和增益。

    参数
    ----
    mvs_python_dir : str | None, optional
        海康 MVS Python SDK 路径。为 None 时使用 controller 内部默认查找逻辑。
    device_index : int, optional
        相机索引。未指定序列号时，按索引选择设备。
    serial_number : str | None, optional
        相机序列号。若提供，则优先按序列号匹配相机。
    exposure_us : int | float | None, optional
        曝光时间，单位微秒。
    gain : float | None, optional
        增益值。

    返回
    ----
    HikCameraController
        已经完成 open 的相机控制对象。

    说明
    ----
    这是 workflow 层面向上层流程提供的“统一开相机入口”。
    与底层 controller 的对齐点是：
    - 曝光调用 set_exposure_us(...)
    - 增益调用 set_gain(...)
    - 支持透传 mvs_python_dir / serial_number

    设计意图是让扫描流程只关心“我要一台可用的相机”，
    而不必关心底层 SDK 细节和对象创建过程。

    注意
    ----
    这里不再静默吞掉曝光/增益设置异常。
    如果参数设置失败，应明确暴露问题，避免“程序看似正常运行，
    实际相机参数没有生效”的隐蔽错误。
    """
    cam = HikCameraController(
        mvs_python_dir=mvs_python_dir,
        device_index=device_index,
        serial_number=serial_number,
    )
    cam.open()

    if exposure_us is not None:
        cam.set_exposure_us(float(exposure_us))

    if gain is not None:
        cam.set_gain(float(gain))

    return cam


def close_camera(cam: HikCameraController | None) -> None:
    """
    安全关闭相机。

    参数
    ----
    cam : HikCameraController | None
        相机对象。为 None 时直接返回。

    说明
    ----
    该函数通常放在 finally 块中调用，用于统一收口资源释放逻辑。
    当前写法保持简单直接：有对象就关闭，没有对象就返回。
    """
    if cam is None:
        return
    cam.close()


def capture_with_opened_camera(
    *,
    cam: HikCameraController,
    save_dir: str,
    filename_pattern: str,
    format_kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """
    使用已打开的相机执行一次拍照，并返回结果信息。

    参数
    ----
    cam : HikCameraController
        已经打开的相机对象。
    save_dir : str
        图像保存目录。
    filename_pattern : str
        图像文件名模板。
    format_kwargs : Dict[str, Any]
        文件名模板所需字段。

    返回
    ----
    Dict[str, Any]
        本次拍照结果，包含：
        - saved_path: 图像保存路径
        - frame: 标准化后的帧信息字典

    说明
    ----
    该函数只负责“已打开相机”条件下的一次采图。
    它不管理相机生命周期，因此适合扫描流程中：
    - 先打开一次相机
    - 连续拍多张
    - 最后再统一关闭

    这样可以避免每张图都 open/close 相机带来的额外开销。
    """
    save_dir_path = Path(save_dir)
    save_dir_path.mkdir(parents=True, exist_ok=True)

    # 先生成当前图像文件名，再拼接完整保存路径
    filename = build_image_name(filename_pattern, format_kwargs=format_kwargs)
    save_path = save_dir_path / filename

    # 调用底层 controller 执行一次软件触发采图
    raw_frame = cam.capture_once(str(save_path))

    # 将底层帧对象转换为更稳定、便于落盘和上层使用的字典结构
    frame_dict = frameinfo_to_dict(raw_frame, str(save_path))
    return {
        "saved_path": str(save_path),
        "frame": frame_dict,
    }


def capture_single_image(
    *,
    save_dir: str,
    filename_pattern: str,
    format_kwargs: Dict[str, Any],
    mvs_python_dir: str | None = None,
    device_index: int = 0,
    serial_number: str | None = None,
    exposure_us: int | float | None = None,
    gain: float | None = None,
) -> Dict[str, Any]:
    """
    执行“打开相机 -> 拍一张 -> 关闭相机”的完整单次采图流程。

    参数
    ----
    save_dir : str
        图像保存目录。
    filename_pattern : str
        图像文件名模板。
    format_kwargs : Dict[str, Any]
        文件名模板所需字段。
    mvs_python_dir : str | None, optional
        海康 MVS Python SDK 路径。
    device_index : int, optional
        相机索引。
    serial_number : str | None, optional
        相机序列号。
    exposure_us : int | float | None, optional
        曝光时间，单位微秒。
    gain : float | None, optional
        增益值。

    返回
    ----
    Dict[str, Any]
        单次采图结果，结构与 capture_with_opened_camera 返回值一致。

    说明
    ----
    这是最适合上层直接调用的单图采集入口，适用于：
    - 调试脚本
    - 接口测试
    - 低频单次拍照任务

    若处于整孔扫描或批量采集场景，更推荐：
    先调用 open_camera()
    然后循环调用 capture_with_opened_camera()
    最后调用 close_camera()

    这样效率更高，也更符合真实设备联机流程。
    """
    cam = None
    try:
        cam = open_camera(
            mvs_python_dir=mvs_python_dir,
            device_index=device_index,
            serial_number=serial_number,
            exposure_us=exposure_us,
            gain=gain,
        )
        return capture_with_opened_camera(
            cam=cam,
            save_dir=save_dir,
            filename_pattern=filename_pattern,
            format_kwargs=format_kwargs,
        )
    finally:
        close_camera(cam)