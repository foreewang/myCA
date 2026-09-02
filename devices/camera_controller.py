from __future__ import annotations
"""
camera_controller.py

功能：
1. 封装海康工业相机 MVS Python SDK 的常用调用流程；
2. 支持按序列号、IP、枚举索引选择相机；
3. 支持软件触发单帧采图；
4. 支持设置曝光、增益；
5. 打开相机时强制设置并回读校验 Mono8；
6. 支持将 Mono8 图像保存为 bmp/jpg/png；
7. 支持 MVS 录像和录像中的快照请求。

适用场景：
- 放在项目的 devices/camera_controller.py 中，作为工程化相机控制模块使用；
- 上层扫描流程、调度流程、测试脚本都只调用本模块，不直接接触 SDK 细节。
"""
import os
import sys
import time
import ctypes
import logging
import importlib
import argparse
import math
import threading
from PIL import Image
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Optional, Dict

# 当前模块日志器
logger = logging.getLogger(__name__)


class CameraSDKError(RuntimeError):
    """相机 SDK 相关异常。"""
    pass

# DEFAULT_MVS_PYTHON_DIR = r"/opt/MVS/Samples/64/Python/MvImport"
DEFAULT_MVS_PYTHON_DIR = r"D:\colony_system\MvImport"
MVS_PIXEL_FORMAT_FALLBACKS = {
    "mono8": 0x01080001,
}
# 与 workflow.api_models.CameraRecordStartRequest.timeout_ms 上限保持一致。
# 15s 覆盖接口允许的最长曝光 10s 加上 GigE 传输余量，禁止再静默截断。
RECORD_GRAB_TIMEOUT_MAX_MS = 15_000
RECORD_GRAB_TRANSFER_SLACK_MS = 2_000
_RECORD_STOP_JOIN_SLACK_S = 10.0
_RECORD_STOP_JOIN_MAX_S = 30.0
_SDK_LOCK_STOP_ACQUIRE_TIMEOUT_S = 2.0
_SDK_STOP_RECORD_TIMEOUT_S = 30.0
_SDK_CLOSE_TIMEOUT_S = 10.0
_SDK_OPEN_TIMEOUT_S = 15.0
_MVS_SDK_LIFECYCLE_LOCK = threading.RLock()
_MVS_SDK_REFCOUNT = 0
_MVS_SDK_INITIALIZED = False


def _check_mvs_ret(ret: int, name: str) -> None:
    if int(ret) != 0:
        raise CameraSDKError(f"{name} failed, ret=0x{int(ret):08x}")


def resolve_record_grab_timeout_ms(
    timeout_ms: Optional[int],
    *,
    exposure_us: Optional[float] = None,
    default_ms: int = 1500,
) -> int:
    """计算录像取帧超时并做显式校验，不静默截断。"""
    exposure_ms = 0
    if exposure_us is not None:
        exposure_value = float(exposure_us)
        if not math.isfinite(exposure_value) or exposure_value <= 0:
            raise CameraSDKError(f"exposure_us must be a finite positive number, got {exposure_us!r}")
        exposure_ms = int((exposure_value + 999.0) / 1000.0)
    min_ms = max(1, exposure_ms + RECORD_GRAB_TRANSFER_SLACK_MS)
    if min_ms > RECORD_GRAB_TIMEOUT_MAX_MS:
        raise CameraSDKError(
            f"exposure {exposure_ms}ms plus {RECORD_GRAB_TRANSFER_SLACK_MS}ms transfer slack "
            f"exceeds maximum recording timeout {RECORD_GRAB_TIMEOUT_MAX_MS}ms"
        )
    if timeout_ms is None:
        return max(int(default_ms), min_ms)
    requested = int(timeout_ms)
    if requested > RECORD_GRAB_TIMEOUT_MAX_MS:
        raise CameraSDKError(
            f"timeout_ms={requested} exceeds maximum {RECORD_GRAB_TIMEOUT_MAX_MS}"
        )
    if requested < min_ms:
        raise CameraSDKError(
            f"timeout_ms={requested} is shorter than exposure {exposure_ms}ms plus "
            f"{RECORD_GRAB_TRANSFER_SLACK_MS}ms transfer slack (need >= {min_ms})"
        )
    return requested


def _acquire_mvs_sdk(MvCamera) -> None:
    """获取进程级 MVS SDK 生命周期引用；首个引用负责 Initialize。"""
    global _MVS_SDK_REFCOUNT, _MVS_SDK_INITIALIZED

    with _MVS_SDK_LIFECYCLE_LOCK:
        if _MVS_SDK_REFCOUNT == 0:
            ret = MvCamera.MV_CC_Initialize()
            _check_mvs_ret(ret, "MV_CC_Initialize")
            _MVS_SDK_INITIALIZED = True
            logger.info("MVS SDK initialized")
        _MVS_SDK_REFCOUNT += 1
        logger.debug("MVS SDK refcount=%s", _MVS_SDK_REFCOUNT)


def _release_mvs_sdk(MvCamera) -> bool:
    """Release one SDK reference and report whether finalization was clean."""
    global _MVS_SDK_REFCOUNT, _MVS_SDK_INITIALIZED

    with _MVS_SDK_LIFECYCLE_LOCK:
        if _MVS_SDK_REFCOUNT <= 0:
            _MVS_SDK_REFCOUNT = 0
            _MVS_SDK_INITIALIZED = False
            return True

        _MVS_SDK_REFCOUNT -= 1
        logger.debug("MVS SDK refcount=%s", _MVS_SDK_REFCOUNT)
        finalized_cleanly = True
        if _MVS_SDK_REFCOUNT == 0 and _MVS_SDK_INITIALIZED:
            try:
                ret = MvCamera.MV_CC_Finalize()
                # Some historical Python wrappers expose the C void return as
                # None.  Preserve compatibility while treating an explicit
                # non-zero SDK result as an unsafe lifecycle.
                if ret is not None and int(ret) != 0:
                    finalized_cleanly = False
                    logger.warning("MV_CC_Finalize failed, ret=0x%08x", int(ret))
                else:
                    logger.info("MVS SDK finalized")
            except Exception:
                finalized_cleanly = False
                logger.exception("MV_CC_Finalize raised during SDK lifecycle release")
            finally:
                _MVS_SDK_INITIALIZED = False
        return finalized_cleanly


@dataclass
class FrameInfo:
    """
    单帧采集完成后返回的信息结构体。

    字段说明：
    - width / height: 图像宽高（像素）
    - frame_num: 帧号
    - pixel_type: 像素格式枚举值
    - frame_len: 原始帧字节长度
    - saved_path: 保存到磁盘的路径
    - timestamp: 保存时刻的时间戳
    """
    width: int
    height: int
    frame_num: int
    pixel_type: int
    frame_len: int
    saved_path: str
    timestamp: float


@dataclass
class VideoRecordInfo:
    """
    录像完成后返回的信息结构体。

    pixel_type 记录启动录像时的相机 PixelFormat 枚举值；当前生产路径期望为 Mono8。
    """
    saved_path: str
    width: int
    height: int
    pixel_type: int
    frame_rate: float
    bitrate_kbps: int
    frame_count: int
    duration_s: float
    timestamp_started: float
    timestamp_finished: float


class HikCameraController:
    """
    海康工业相机控制器。

    设计目标：
    - 让上层代码只关心“打开相机 / 设置参数 / 采图 / 关闭相机”
    - 将 SDK 导入、设备枚举、句柄创建、参数校验、异常清理都收敛在这里
    - 打开过程中任一步失败都会释放已经创建的 SDK 资源，避免占用相机

    相机选择优先级：
    1. serial_number
    2. camera_ip
    3. device_index

    MVS Python 模块默认从以下优先级寻找：
    1. 构造参数 mvs_python_dir
    2. 环境变量 MVS_PYTHON_DIR
    3. DEFAULT_MVS_PYTHON_DIR
    """

    def __init__(
        self,
        mvs_python_dir: Optional[str] = None,
        device_index: int = 0,
        serial_number: Optional[str] = None,
        camera_ip: Optional[str] = None,
        trigger_source: str = "software",
        pixel_format: str = "mono8",
        grab_timeout_ms: int = 1500,
        jpg_quality: int = 90,
        default_exposure_us: Optional[float] = None,
        default_gain: Optional[float] = None,
    ) -> None:
        # MVS Python 模块目录
        self.mvs_python_dir = mvs_python_dir or os.getenv("MVS_PYTHON_DIR") or DEFAULT_MVS_PYTHON_DIR
        # 相机选择优先级：serial_number > camera_ip > device_index。
        self.device_index = device_index
        self.serial_number = serial_number
        self.camera_ip = str(camera_ip or "").strip() or None
        # 当前仅实现 software 软件触发
        self.trigger_source = trigger_source.lower().strip()
        self.pixel_format = self._normalize_pixel_format(pixel_format)
        # 获取单帧时的等待超时，单位 ms
        self.grab_timeout_ms = int(grab_timeout_ms)
        # 兼容旧版 SDK 保存接口的 jpg 质量参数；当前 Pillow 保存路径暂未使用。
        self.jpg_quality = int(jpg_quality)
        # 打开相机后若提供默认曝光 / 增益，则自动设置
        self.default_exposure_us = default_exposure_us
        self.default_gain = default_gain

        # _sdk_loaded 只表示 Python 包已导入；_sdk_initialized 表示本实例持有 MVS 全局生命周期引用。
        self._sdk_loaded = False
        self._sdk_initialized = False

        # 用字典统一保存导入到的 SDK 类、结构体、常量
        self._sdk: Dict[str, Any] = {}

        # 相机实例句柄对象（MvCamera）
        self.cam: Any = None

        # 当前相机 PayloadSize（每帧最大数据长度）
        self.payload_size: Optional[int] = None

        # 当前选中的设备信息结构体
        self.device_info: Any = None

        # 状态标志
        self.opened = False
        self.grabbing = False
        self.recording = False
        # 串行化所有直接访问 self.cam / MVS SDK 句柄的公共入口。
        self._sdk_lock = threading.RLock()
        self._record_stop_event = threading.Event()
        self._record_thread: Optional[threading.Thread] = None
        self._snapshot_condition = threading.Condition()
        self._snapshot_requests: list[Dict[str, Any]] = []
        self._record_path: Optional[str] = None
        self._record_started_at: Optional[float] = None
        self._record_width = 0
        self._record_height = 0
        self._record_pixel_type = 0
        self._record_frame_rate = 0.0
        self._record_bitrate_kbps = 0
        self._record_frame_count = 0
        self._record_error: Optional[str] = None
        self._record_continuous_mode = False
        self._record_grab_timeout_ms = int(grab_timeout_ms)
        self._record_snapshot_accepting = False
        self._sdk_hung = False
        self._closed = False
        self._handle_created = False
        self._device_opened = False
        # PayloadSize 通常约为 25 MiB。缓存 ctypes 数组，避免录像每帧重新分配并清零。
        self._grab_buffer: Any = None
        self._grab_buffer_size = 0

    @staticmethod
    def _normalize_pixel_format(pixel_format: str | None) -> str:
        """归一化配置中的像素格式名称；当前生产流程只允许 Mono8。"""
        text = str(pixel_format or "mono8").strip().lower().replace("-", "").replace("_", "")
        if text in {"mono8", "monochrome8"}:
            return "mono8"
        raise CameraSDKError(f"unsupported pixel_format={pixel_format!r}; only mono8 is supported")

    def _load_sdk(self) -> None:
        """
        动态导入海康 MVS Python SDK。

        这样写的好处：
        - 不要求用户必须把 SDK 路径提前配到系统环境变量
        - 只在真正需要打开相机时才导入 SDK
        """
        if self._sdk_loaded:
            return

        candidate_dirs = []
        if self.mvs_python_dir:
            candidate_dirs.append(self.mvs_python_dir)

        for d in candidate_dirs:
            if d and d not in sys.path:
                sys.path.insert(0, d)

        try:
            mv_mod = importlib.import_module("MvCameraControl_class")
            hdr_mod = importlib.import_module("CameraParams_header")
            const_mod = importlib.import_module("CameraParams_const")
        except Exception as e:
            raise CameraSDKError(
                "无法导入海康 MVS Python 模块。请确认 MvImport 路径正确。\n"
                f"当前尝试路径: {self.mvs_python_dir}\n"
                "建议检查：\n"
                "1) MVS 已安装；\n"
                "2) 目录中存在 MvCameraControl_class.py、CameraParams_header.py 和 CameraParams_const.py；\n"
                "3) 该目录已通过构造参数或环境变量 MVS_PYTHON_DIR 传入。"
            ) from e

        # 部分 MVS 包把 PixelType_Gvsp_* 放在主模块中，因此同时收集 const/header 以外的常量。
        consts: Dict[str, Any] = {}
        for module in (const_mod, mv_mod):
            for name in dir(module):
                if name.startswith(("MV_", "PixelType_Gvsp_")):
                    consts.setdefault(name, getattr(module, name))

        sdk = {
            "MvCamera": getattr(mv_mod, "MvCamera"),
            "MV_CC_DEVICE_INFO_LIST": getattr(hdr_mod, "MV_CC_DEVICE_INFO_LIST"),
            "MV_CC_DEVICE_INFO": getattr(hdr_mod, "MV_CC_DEVICE_INFO"),
            "MVCC_INTVALUE": getattr(hdr_mod, "MVCC_INTVALUE", None),
            "MVCC_ENUMVALUE": getattr(hdr_mod, "MVCC_ENUMVALUE", None),
            "MVCC_FLOATVALUE": getattr(hdr_mod, "MVCC_FLOATVALUE", None),
            "MV_FRAME_OUT_INFO_EX": getattr(hdr_mod, "MV_FRAME_OUT_INFO_EX"),
            "MV_SAVE_IMAGE_PARAM_EX": getattr(hdr_mod, "MV_SAVE_IMAGE_PARAM_EX"),
            "MV_CC_RECORD_PARAM": getattr(hdr_mod, "MV_CC_RECORD_PARAM", None),
            "MV_CC_INPUT_FRAME_INFO": getattr(hdr_mod, "MV_CC_INPUT_FRAME_INFO", None),
            **consts,
        }
        self._sdk = sdk
        self._sdk_loaded = True

    @staticmethod
    def _decode_c_char_array(raw: Any) -> str:
        try:
            if isinstance(raw, (bytes, bytearray)):
                return bytes(raw).split(b"\x00", 1)[0].decode(errors="ignore")
            if hasattr(raw, "value"):
                return raw.value.decode(errors="ignore")
            return bytes(raw).split(b"\x00", 1)[0].decode(errors="ignore")
        except Exception:
            return ""

    def _check(self, ret: int, name: str) -> None:
        if int(ret) != 0:
            raise CameraSDKError(f"{name} failed, ret=0x{int(ret):08x}")

    def _ensure_sdk_healthy(self, operation: str = "MVS SDK operation") -> None:
        """拒绝在已超时的原生调用仍可能存活时再次进入同一 SDK 实例。"""
        if self._sdk_hung:
            raise CameraSDKError(
                f"{operation} skipped: a previous MVS SDK call is still hung; "
                "restart the camera process before retrying"
            )

    def _call_variants(self, func, variants, func_name: str):
        last_exc = None
        for args in variants:
            self._ensure_sdk_healthy(func_name)
            try:
                return func(*args)
            except TypeError as e:
                last_exc = e
                continue
        raise CameraSDKError(f"{func_name} 调用失败，当前 MVS Python 包装签名可能与脚本不一致: {last_exc}")

    def _enum_devices(self):
        MvCamera = self._sdk["MvCamera"]
        MV_CC_DEVICE_INFO_LIST = self._sdk["MV_CC_DEVICE_INFO_LIST"]

        dev_list = MV_CC_DEVICE_INFO_LIST()
        ctypes.memset(ctypes.byref(dev_list), 0, ctypes.sizeof(dev_list))

        device_mask = 0
        for key in (
            "MV_GIGE_DEVICE",
            "MV_USB_DEVICE",
            "MV_GENTL_GIGE_DEVICE",
            "MV_GENTL_CAMERALINK_DEVICE",
            "MV_GENTL_CXP_DEVICE",
            "MV_GENTL_XOF_DEVICE",
        ):
            device_mask |= int(self._sdk.get(key, 0))
        if device_mask == 0:
            device_mask = (1 << 0) | (1 << 1)

        self._timed_sdk_call(
            lambda: self._check(
                self._call_variants(
                    MvCamera.MV_CC_EnumDevices,
                    [
                        (device_mask, dev_list),
                        (device_mask, ctypes.byref(dev_list)),
                    ],
                    "MV_CC_EnumDevices",
                ),
                "MV_CC_EnumDevices",
            ),
            _SDK_OPEN_TIMEOUT_S,
            "MV_CC_EnumDevices",
        )
        return dev_list

    def _get_device_serial(self, dev_info) -> str:
        tlayer = int(getattr(dev_info, "nTLayerType", -1))
        gige_const = int(self._sdk.get("MV_GIGE_DEVICE", -999))
        usb_const = int(self._sdk.get("MV_USB_DEVICE", -998))

        try:
            if tlayer == gige_const:
                info = dev_info.SpecialInfo.stGigEInfo
                return self._decode_c_char_array(getattr(info, "chSerialNumber", b""))
            if tlayer == usb_const:
                info = dev_info.SpecialInfo.stUsb3VInfo
                return self._decode_c_char_array(getattr(info, "chSerialNumber", b""))
        except Exception:
            pass
        return ""

    @staticmethod
    def _int_to_ipv4(value: Any) -> str:
        try:
            n = int(value)
        except Exception:
            return ""
        return ".".join(str((n >> shift) & 0xFF) for shift in (24, 16, 8, 0))

    def _get_device_ip(self, dev_info) -> str:
        """读取 GigE 设备当前 IP；非 GigE 或读取失败时返回空字符串。"""
        try:
            info = dev_info.SpecialInfo.stGigEInfo
            return self._int_to_ipv4(getattr(info, "nCurrentIp", 0))
        except Exception:
            return ""

    def _select_device(self, dev_list):
        """按 serial_number > camera_ip > device_index 的优先级选择设备。"""
        n = int(getattr(dev_list, "nDeviceNum", 0))
        if n <= 0:
            raise CameraSDKError("未枚举到相机设备")

        MV_CC_DEVICE_INFO = self._sdk["MV_CC_DEVICE_INFO"]
        matched = None
        for i in range(n):
            ptr = dev_list.pDeviceInfo[i]
            dev_info = ctypes.cast(ptr, ctypes.POINTER(MV_CC_DEVICE_INFO)).contents
            serial = self._get_device_serial(dev_info)
            ip = self._get_device_ip(dev_info)
            logger.info("camera device[%s] serial=%s ip=%s", i, serial or "<unknown>", ip or "<unknown>")
            if self.serial_number and serial == self.serial_number:
                matched = dev_info
                break
            if not self.serial_number and self.camera_ip and ip == self.camera_ip:
                matched = dev_info
                break

        if matched is not None:
            return matched
        if self.serial_number:
            raise CameraSDKError(f"未找到序列号为 {self.serial_number} 的相机")
        if self.camera_ip:
            raise CameraSDKError(f"未找到 IP 为 {self.camera_ip} 的相机")
        if not (0 <= self.device_index < n):
            raise CameraSDKError(f"device_index={self.device_index} 超出范围，当前仅有 {n} 台设备")

        return ctypes.cast(
            dev_list.pDeviceInfo[self.device_index],
            ctypes.POINTER(MV_CC_DEVICE_INFO),
        ).contents

    def _set_enum(self, key: str, value: int) -> None:
        ret = self._call_variants(
            self.cam.MV_CC_SetEnumValue,
            [
                (key, int(value)),
                (key.encode("ascii"), int(value)),
            ],
            f"MV_CC_SetEnumValue({key})",
        )
        self._check(ret, f"MV_CC_SetEnumValue({key})")

    def _set_command(self, key: str) -> None:
        ret = self._call_variants(
            self.cam.MV_CC_SetCommandValue,
            [
                (key,),
                (key.encode("ascii"),),
            ],
            f"MV_CC_SetCommandValue({key})",
        )
        self._check(ret, f"MV_CC_SetCommandValue({key})")

    def _set_float(self, key: str, value: float) -> None:
        if not hasattr(self.cam, "MV_CC_SetFloatValue"):
            raise CameraSDKError("当前 MVS Python 包装中不存在 MV_CC_SetFloatValue，无法设置浮点参数")
        ret = self._call_variants(
            self.cam.MV_CC_SetFloatValue,
            [
                (key, float(value)),
                (key.encode("ascii"), float(value)),
            ],
            f"MV_CC_SetFloatValue({key})",
        )
        self._check(ret, f"MV_CC_SetFloatValue({key})")

    def _set_bool(self, key: str, value: bool) -> None:
        if not hasattr(self.cam, "MV_CC_SetBoolValue"):
            raise CameraSDKError("当前 MVS Python 包装中不存在 MV_CC_SetBoolValue")
        ret = self._call_variants(
            self.cam.MV_CC_SetBoolValue,
            [
                (key, bool(value)),
                (key.encode("ascii"), bool(value)),
            ],
            f"MV_CC_SetBoolValue({key})",
        )
        self._check(ret, f"MV_CC_SetBoolValue({key})")

    def _timed_sdk_call(self, func, timeout_s: float, name: str):
        """在辅助线程执行原生 SDK 调用，超时则标记挂起并让调用方返回错误。

        无法真正中止 ctypes 调用；挂起后拒绝后续 SDK 操作，直到进程重启。
        """
        if self._sdk_hung:
            raise CameraSDKError(f"{name} skipped: a previous MVS SDK call is still hung")
        box: Dict[str, Any] = {}

        def runner() -> None:
            try:
                box["result"] = func()
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=runner, name=f"mvs-{name}", daemon=True)
        thread.start()
        thread.join(timeout=max(float(timeout_s), 0.1))
        if thread.is_alive():
            self._sdk_hung = True
            message = f"{name} timed out after {timeout_s}s; camera SDK may be hung"
            self._record_error = message
            raise CameraSDKError(message)
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def _get_int_value(self, key: str) -> int:
        MVCC_INTVALUE = self._sdk["MVCC_INTVALUE"]
        if MVCC_INTVALUE is None:
            raise CameraSDKError("当前 MVS Python 包装中不存在 MVCC_INTVALUE")
        st = MVCC_INTVALUE()
        ctypes.memset(ctypes.byref(st), 0, ctypes.sizeof(st))
        ret = self._call_variants(
            self.cam.MV_CC_GetIntValue,
            [
                (key, st),
                (key, ctypes.byref(st)),
                (key.encode("ascii"), st),
                (key.encode("ascii"), ctypes.byref(st)),
            ],
            f"MV_CC_GetIntValue({key})",
        )
        self._check(ret, f"MV_CC_GetIntValue({key})")
        return int(getattr(st, "nCurValue", 0))

    def _get_float_value(self, key: str) -> float:
        MVCC_FLOATVALUE = self._sdk["MVCC_FLOATVALUE"]
        if MVCC_FLOATVALUE is None or not hasattr(self.cam, "MV_CC_GetFloatValue"):
            raise CameraSDKError("当前 MVS Python 包装不支持浮点参数读取")
        st = MVCC_FLOATVALUE()
        ctypes.memset(ctypes.byref(st), 0, ctypes.sizeof(st))
        ret = self._call_variants(
            self.cam.MV_CC_GetFloatValue,
            [
                (key, st),
                (key, ctypes.byref(st)),
                (key.encode("ascii"), st),
                (key.encode("ascii"), ctypes.byref(st)),
            ],
            f"MV_CC_GetFloatValue({key})",
        )
        self._check(ret, f"MV_CC_GetFloatValue({key})")
        # 新旧结构字段名可能不同
        for attr in ("fCurValue", "nCurValue", "curValue"):
            if hasattr(st, attr):
                return float(getattr(st, attr))
        raise CameraSDKError(f"读取 {key} 成功，但未找到当前值字段")

    def _get_enum_value(self, key: str) -> int:
        MVCC_ENUMVALUE = self._sdk["MVCC_ENUMVALUE"]
        if MVCC_ENUMVALUE is None or not hasattr(self.cam, "MV_CC_GetEnumValue"):
            raise CameraSDKError("当前 MVS Python 包装不支持枚举参数读取")
        st = MVCC_ENUMVALUE()
        ctypes.memset(ctypes.byref(st), 0, ctypes.sizeof(st))
        ret = self._call_variants(
            self.cam.MV_CC_GetEnumValue,
            [
                (key, st),
                (key, ctypes.byref(st)),
                (key.encode("ascii"), st),
                (key.encode("ascii"), ctypes.byref(st)),
            ],
            f"MV_CC_GetEnumValue({key})",
        )
        self._check(ret, f"MV_CC_GetEnumValue({key})")
        return int(getattr(st, "nCurValue", 0))

    def _pixel_format_value(self, pixel_format: str | None = None) -> int:
        """返回 MVS SDK 的 PixelFormat 枚举值；当前只支持 Mono8。"""
        normalized = self._normalize_pixel_format(pixel_format or self.pixel_format)
        if normalized == "mono8":
            return int(self._sdk.get("PixelType_Gvsp_Mono8", MVS_PIXEL_FORMAT_FALLBACKS["mono8"]))
        raise CameraSDKError(f"unsupported pixel_format={pixel_format!r}")

    def _set_pixel_format(self) -> None:
        """设置相机 PixelFormat 并回读校验，避免配置未真正生效。"""
        expected = self._pixel_format_value(self.pixel_format)
        self._set_enum("PixelFormat", expected)
        actual = self._get_enum_value("PixelFormat")
        if actual != expected:
            raise CameraSDKError(
                f"PixelFormat readback mismatch: expected {self.pixel_format}({expected}), actual={actual}"
            )
        logger.info("set PixelFormat=%s (%s)", self.pixel_format, expected)

    def _is_expected_pixel_type(self, pixel_type: int) -> bool:
        """判断采集帧的实际像素格式是否符合当前配置。"""
        return int(pixel_type) == self._pixel_format_value(self.pixel_format)

    def _validate_mono8_frame_info(self, frame_info, *, context: str) -> tuple[int, int, int, int, int]:
        """
        校验帧信息是否符合当前生产路径要求的 Mono8。

        只检查帧元数据，不扫描或拷贝图像数据，因此可用于录像逐帧校验。
        返回 width、height、frame_len、pixel_type、frame_num。
        """
        width = int(getattr(frame_info, "nWidth", 0))
        height = int(getattr(frame_info, "nHeight", 0))
        frame_len = int(getattr(frame_info, "nFrameLen", 0))
        pixel_type = int(getattr(frame_info, "enPixelType", 0))
        frame_num = int(getattr(frame_info, "nFrameNum", 0))

        if width <= 0 or height <= 0 or frame_len <= 0:
            raise CameraSDKError(
                f"{context} frame info invalid: width={width}, height={height}, "
                f"frame_len={frame_len}, pixel_type={pixel_type}, frame_num={frame_num}"
            )

        if not self._is_expected_pixel_type(pixel_type):
            raise CameraSDKError(
                f"{context} PixelFormat mismatch: expected {self.pixel_format}, actual pixel_type={pixel_type}, "
                f"width={width}, height={height}, frame_len={frame_len}, frame_num={frame_num}"
            )

        expected_len = width * height
        if frame_len != expected_len:
            raise CameraSDKError(
                f"{context} Mono8 frame length mismatch: expected {expected_len}, actual={frame_len}, "
                f"pixel_type={pixel_type}, frame_num={frame_num}"
            )

        return width, height, frame_len, pixel_type, frame_num

    def _cleanup_after_open_failure(self, step: str) -> None:
        """open() 中途失败时释放已创建的句柄和 SDK 全局状态。"""
        logger.error("camera open failed during %s; cleaning up partial resources", step)
        if not self.close():
            logger.error(
                "camera cleanup after %s did not finish; native handle may still be live",
                step,
            )

    def _run_open_step(self, step: str, func, *args):
        """执行 open() 的一个阶段；失败时统一清理，防止相机句柄残留。"""
        try:
            return func(*args)
        except Exception:
            self._cleanup_after_open_failure(step)
            raise

    def _run_timed_open_native_step(self, step: str, timeout_s: float, func, *, flag: str) -> None:
        """带超时的打开阶段。超时后按“句柄可能已创建”处理，避免后续误报空闲。"""
        try:
            self._timed_sdk_call(func, timeout_s, step)
            if flag == "handle":
                self._handle_created = True
            elif flag == "device":
                self._device_opened = True
        except Exception:
            if self._sdk_hung:
                if flag == "handle":
                    self._handle_created = True
                elif flag == "device":
                    self._device_opened = True
            self._cleanup_after_open_failure(step)
            raise

    def _set_int(self, key: str, value: int) -> None:
        if not hasattr(self.cam, "MV_CC_SetIntValue"):
            raise CameraSDKError("当前 MVS Python 包装中不存在 MV_CC_SetIntValue")
        ret = self._call_variants(
            self.cam.MV_CC_SetIntValue,
            [
                (key, int(value)),
                (key.encode("ascii"), int(value)),
            ],
            f"MV_CC_SetIntValue({key})",
        )
        self._check(ret, f"MV_CC_SetIntValue({key})")

    def open(self) -> None:
        with self._sdk_lock:
            self._ensure_sdk_healthy("open camera")
            # close() is idempotent, but a controller may be reopened by
            # legacy scan code.  A new successful lifecycle must not inherit
            # the previous close sentinel.
            self._closed = False
            self._open_unlocked()

    def _open_unlocked(self) -> None:
        """
        打开相机并完成基础初始化。

        该方法是原子化打开流程：从获取 SDK 生命周期引用到 PayloadSize 的任一步失败，
        都会调用 close() 清理已创建的句柄、取流状态和 SDK 全局初始化状态。

        执行顺序：
        1. 导入 SDK
        2. 创建 MvCamera 实例
        3. 获取 SDK 全局生命周期引用（首个引用会执行 MV_CC_Initialize）
        4. 枚举并选择设备
        5. 创建句柄
        6. 打开设备
        7. 配置网络包大小（GigE 可优化）
        8. 设置并回读校验 PixelFormat
        9. 设置触发模式
        10. 设置默认曝光、增益
        11. 开始取流
        12. 读取 PayloadSize
        """
        if self.opened:
            return

        # close() 会把该标志置 True；复用同一个 controller 重新 open 时必须开启新生命周期。
        self._closed = False

        self._load_sdk()
        MvCamera = self._sdk["MvCamera"]
        self.cam = MvCamera()

        self._run_open_step("MVS SDK lifecycle acquire", _acquire_mvs_sdk, MvCamera)
        self._sdk_initialized = True

        dev_list = self._run_open_step("MV_CC_EnumDevices", self._enum_devices)
        self.device_info = self._run_open_step("select_device", self._select_device, dev_list)

        self._run_timed_open_native_step(
            "MV_CC_CreateHandle",
            _SDK_OPEN_TIMEOUT_S,
            lambda: self._check(
                self._call_variants(
                    self.cam.MV_CC_CreateHandle,
                    [
                        (self.device_info,),
                        (ctypes.byref(self.device_info),),
                    ],
                    "MV_CC_CreateHandle",
                ),
                "MV_CC_CreateHandle",
            ),
            flag="handle",
        )

        access_exclusive = int(self._sdk.get("MV_ACCESS_Exclusive", 1))
        self._run_timed_open_native_step(
            "MV_CC_OpenDevice",
            _SDK_OPEN_TIMEOUT_S,
            lambda: self._check(
                self._call_variants(
                    self.cam.MV_CC_OpenDevice,
                    [
                        (access_exclusive, 0),
                        (),
                    ],
                    "MV_CC_OpenDevice",
                ),
                "MV_CC_OpenDevice",
            ),
            flag="device",
        )

        self._run_open_step("GevSCPSPacketSize", self._try_set_optimal_packet_size)
        self._run_open_step("PixelFormat", self._set_pixel_format)
        self._run_open_step("TriggerMode", self._set_trigger_mode)

        if self.default_exposure_us is not None:
            self._run_open_step("ExposureTime", self._set_exposure_us_unlocked, self.default_exposure_us)
        if self.default_gain is not None:
            self._run_open_step("Gain", self._set_gain_unlocked, self.default_gain)

        self._run_open_step("MV_CC_StartGrabbing", self._start_grabbing_unlocked)
        self.payload_size = self._run_open_step("PayloadSize", self._get_int_value, "PayloadSize")
        if self.payload_size <= 0:
            self._cleanup_after_open_failure("PayloadSize")
            raise CameraSDKError("PayloadSize 获取失败")

        self.opened = True
        logger.info("camera opened, payload_size=%s", self.payload_size)

    def _try_set_optimal_packet_size(self) -> None:
        try:
            self._ensure_sdk_healthy("MV_CC_GetOptimalPacketSize")
            packet_size = int(self.cam.MV_CC_GetOptimalPacketSize())
            if packet_size > 0:
                try:
                    self._set_int("GevSCPSPacketSize", packet_size)
                except Exception as e:
                    logger.warning("设置最佳网络包大小失败: %s", e)
        except Exception:
            pass

    def _set_trigger_mode(self) -> None:
        """
        设置触发模式。

        当前脚本逻辑：
        - TriggerMode = On
        - TriggerSource = Software

        即：不是连续采集，而是每次调用 capture_once 时，
        通过 TriggerSoftware 主动触发一次拍照。
        """
        self._set_enum("TriggerMode", 1)
        if self.trigger_source == "software":
            self._set_enum("TriggerSource", int(self._sdk.get("MV_TRIGGER_SOURCE_SOFTWARE", 7)))
        else:
            raise CameraSDKError(f"当前脚本只实现 software 触发，收到 trigger_source={self.trigger_source}")

    def _configure_trigger_mode_unlocked(self, *, continuous: bool, fps: Optional[float] = None) -> None:
        """录像时切到连续取流，结束后恢复软件触发；切换期间会短暂 Stop/StartGrabbing。"""
        self._ensure_sdk_healthy("configure trigger mode")
        continuous = bool(continuous)
        if bool(self._record_continuous_mode) == continuous and not continuous:
            return
        was_grabbing = self.grabbing
        try:
            if was_grabbing:
                self._stop_grabbing_unlocked()
            if continuous:
                self._set_enum("TriggerMode", int(self._sdk.get("MV_TRIGGER_MODE_OFF", 0)))
                self._record_continuous_mode = True
                self._apply_recording_stream_unlocked(float(fps or self._record_frame_rate or 10.0))
                logger.info("camera trigger mode set to continuous for recording")
            else:
                self._set_trigger_mode()
                self._record_continuous_mode = False
                logger.info("camera trigger mode restored to software")
        except Exception as exc:
            # timed SDK call 的辅助线程仍可能在原生库内运行；此时绝不能再进入 SDK 做回滚。
            if self._sdk_hung:
                raise
            try:
                self._set_trigger_mode()
                self._record_continuous_mode = False
            except Exception as restore_exc:
                logger.exception("failed to restore software trigger after trigger mode change error")
                raise CameraSDKError(
                    "continuous acquisition setup failed and software-trigger rollback also failed"
                ) from restore_exc
            raise
        finally:
            if was_grabbing and not self.grabbing and not self._sdk_hung:
                self._start_grabbing_unlocked()
                if self._record_continuous_mode:
                    self._drain_stale_frames_unlocked()

    def _apply_recording_stream_unlocked(self, fps: float) -> None:
        """严格配置连续录像帧率和最新帧策略；任一步失败均由调用方回退。"""
        if fps <= 0:
            raise CameraSDKError(f"recording fps must be positive, got {fps}")
        enable_set = False
        enable_errors = []
        for setter in (
            lambda: self._set_bool("AcquisitionFrameRateEnable", True),
            lambda: self._set_enum("AcquisitionFrameRateEnable", 1),
            lambda: self._set_int("AcquisitionFrameRateEnable", 1),
        ):
            try:
                setter()
                enable_set = True
                break
            except Exception as exc:
                if self._sdk_hung:
                    raise
                enable_errors.append(str(exc))
        if not enable_set:
            raise CameraSDKError(
                "failed to enable AcquisitionFrameRate: " + "; ".join(enable_errors)
            )

        self._set_float("AcquisitionFrameRate", float(fps))

        strategy = self._sdk.get("MV_GrabStrategy_LatestImagesOnly")
        if strategy is None:
            strategy = self._sdk.get("MV_GrabStrategy_LatestImages")
        if strategy is None:
            raise CameraSDKError("MVS SDK does not expose a latest-frame grab strategy")
        if not hasattr(self.cam, "MV_CC_SetGrabStrategy"):
            raise CameraSDKError("current MVS wrapper does not support MV_CC_SetGrabStrategy")

        ret = self._call_variants(
            self.cam.MV_CC_SetGrabStrategy,
            [(int(strategy),), (int(strategy), 1)],
            "MV_CC_SetGrabStrategy",
        )
        self._check(ret, "MV_CC_SetGrabStrategy")
        logger.info(
            "continuous recording stream configured: fps=%s latest_strategy=%s",
            fps,
            strategy,
        )

    def _drain_stale_frames_unlocked(self, *, max_frames: int = 4, timeout_ms: int = 1) -> None:
        if not self._record_continuous_mode or not self.grabbing:
            return
        drained = 0
        for _ in range(max_frames):
            try:
                self._grab_one_frame(timeout_ms, send_software_trigger=False)
                drained += 1
            except Exception:
                break
        if drained:
            logger.info("drained %s stale frame(s) from continuous stream", drained)

    def _resolve_record_grab_timeout_ms(self, timeout_ms: Optional[int]) -> int:
        return resolve_record_grab_timeout_ms(
            timeout_ms,
            exposure_us=self.default_exposure_us,
            default_ms=self.grab_timeout_ms,
        )

    def _join_timeout_for_recording_stop(self, join_timeout_s: float) -> float:
        grab_s = max(int(self._record_grab_timeout_ms or self.grab_timeout_ms), 1) / 1000.0
        needed = grab_s + _RECORD_STOP_JOIN_SLACK_S
        return min(max(float(join_timeout_s), needed, 0.1), _RECORD_STOP_JOIN_MAX_S)

    def start_grabbing(self) -> None:
        with self._sdk_lock:
            self._start_grabbing_unlocked()

    def _start_grabbing_unlocked(self) -> None:
        """开始取流。"""
        if self.grabbing:
            return
        self._timed_sdk_call(
            lambda: self._check(self.cam.MV_CC_StartGrabbing(), "MV_CC_StartGrabbing"),
            _SDK_CLOSE_TIMEOUT_S,
            "MV_CC_StartGrabbing",
        )
        self.grabbing = True

    def stop_grabbing(self) -> None:
        with self._sdk_lock:
            self._stop_grabbing_unlocked()

    def _stop_grabbing_unlocked(self) -> None:
        """停止取流。"""
        if self.cam is None or not self.grabbing:
            return
        self._timed_sdk_call(
            lambda: self._check(self.cam.MV_CC_StopGrabbing(), "MV_CC_StopGrabbing"),
            _SDK_CLOSE_TIMEOUT_S,
            "MV_CC_StopGrabbing",
        )
        self.grabbing = False

    def set_exposure_us(self, exposure_us: float) -> None:
        with self._sdk_lock:
            self._set_exposure_us_unlocked(exposure_us)

    def set_exposure_auto(self, enabled: bool) -> None:
        """Enable or disable continuous automatic exposure."""
        with self._sdk_lock:
            self._ensure_sdk_healthy("set automatic exposure")
            value = int(
                self._sdk.get("MV_EXPOSURE_AUTO_MODE_CONTINUOUS", 2)
                if bool(enabled)
                else self._sdk.get("MV_EXPOSURE_AUTO_MODE_OFF", 0)
            )
            self._set_enum("ExposureAuto", value)
            if enabled:
                self.default_exposure_us = None

    def _set_exposure_us_unlocked(self, exposure_us: float) -> None:
        """设置曝光时间，单位微秒。会先关闭自动曝光。"""
        try:
            self._set_enum("ExposureAuto", int(self._sdk.get("MV_EXPOSURE_AUTO_MODE_OFF", 0)))
        except Exception:
            try:
                self._set_enum("ExposureMode", int(self._sdk.get("MV_EXPOSURE_MODE_TIMED", 0)))
            except Exception:
                pass
        self._set_float("ExposureTime", float(exposure_us))
        self.default_exposure_us = float(exposure_us)
        logger.info("set exposure_us=%s", exposure_us)

    def get_exposure_us(self) -> float:
        with self._sdk_lock:
            return self._get_float_value("ExposureTime")

    def set_gain(self, gain: float) -> None:
        with self._sdk_lock:
            self._set_gain_unlocked(gain)

    def _set_gain_unlocked(self, gain: float) -> None:
        """设置增益，单位通常为 dB。会先关闭自动增益。"""
        try:
            self._set_enum("GainAuto", int(self._sdk.get("MV_GAIN_MODE_OFF", 0)))
        except Exception:
            pass
        self._set_float("Gain", float(gain))
        logger.info("set gain=%s", gain)

    def get_gain(self) -> float:
        with self._sdk_lock:
            return self._get_float_value("Gain")

    @property
    def is_background_recording(self) -> bool:
        thread = self._record_thread
        return bool(self.recording and thread is not None and thread.is_alive())

    def _is_background_recording_unlocked(self) -> bool:
        thread = self._record_thread
        return bool(self.recording and thread is not None and thread.is_alive())

    def _fail_pending_snapshot_requests(self, error: Any) -> None:
        with self._snapshot_condition:
            self._fail_pending_snapshot_requests_unlocked(error)

    def _fail_pending_snapshot_requests_unlocked(self, error: Any) -> None:
        if not self._snapshot_requests:
            self._snapshot_condition.notify_all()
            return
        for request in self._snapshot_requests:
            request["error"] = error
            request["done"] = True
        self._snapshot_requests.clear()
        self._snapshot_condition.notify_all()

    def _grab_one_frame(self, timeout_ms: int, *, send_software_trigger: bool | None = None):
        if send_software_trigger is None:
            send_software_trigger = self.trigger_source == "software" and not self._record_continuous_mode
        if send_software_trigger:
            self._set_command("TriggerSoftware")

        MV_FRAME_OUT_INFO_EX = self._sdk["MV_FRAME_OUT_INFO_EX"]
        frame_info = MV_FRAME_OUT_INFO_EX()
        ctypes.memset(ctypes.byref(frame_info), 0, ctypes.sizeof(frame_info))

        payload_size = int(self.payload_size)
        if self._grab_buffer is None or self._grab_buffer_size != payload_size:
            self._grab_buffer = (ctypes.c_ubyte * payload_size)()
            self._grab_buffer_size = payload_size
        data_buf = self._grab_buffer
        ret = self._call_variants(
            self.cam.MV_CC_GetOneFrameTimeout,
            [
                (data_buf, int(self.payload_size), frame_info, timeout_ms),
                (ctypes.byref(data_buf), int(self.payload_size), frame_info, timeout_ms),
                (data_buf, int(self.payload_size), ctypes.byref(frame_info), timeout_ms),
                (ctypes.byref(data_buf), int(self.payload_size), ctypes.byref(frame_info), timeout_ms),
            ],
            "MV_CC_GetOneFrameTimeout",
        )
        self._check(ret, "MV_CC_GetOneFrameTimeout")
        return data_buf, frame_info

    def capture_snapshot_during_recording(
        self,
        save_path: str,
        timeout_ms: Optional[int] = None,
    ) -> FrameInfo:
        """
        后台录像过程中请求保存一张快照。

        录像线程在下一帧到达时复用同一帧数据保存图片，避免同时调用
        MV_CC_GetOneFrameTimeout 导致 SDK 取流竞争。
        """
        if timeout_ms is None:
            frame_wait_ms = 0
            if self._record_frame_rate > 0:
                frame_wait_ms = int(2000.0 / max(float(self._record_frame_rate), 0.1))
            timeout_ms = max(int(self.grab_timeout_ms), int(self._record_grab_timeout_ms or 0), frame_wait_ms)
        timeout_ms = int(timeout_ms)
        request: Dict[str, Any] = {
            "save_path": str(Path(save_path)),
            "done": False,
            "frame": None,
            "error": None,
        }
        Path(request["save_path"]).parent.mkdir(parents=True, exist_ok=True)

        with self._snapshot_condition:
            if not self._record_snapshot_accepting:
                raise CameraSDKError(
                    "background recording is not running; "
                    "cannot capture snapshot from recording frame"
                )
            self._snapshot_requests.append(request)
            self._snapshot_condition.notify_all()

        deadline = time.monotonic() + max(timeout_ms / 1000.0, 0.1)
        with self._snapshot_condition:
            while not request["done"]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    try:
                        self._snapshot_requests.remove(request)
                    except ValueError:
                        pass
                    raise CameraSDKError(f"录像中拍照等待超时: {request['save_path']}")
                self._snapshot_condition.wait(timeout=remaining)

        if request["error"] is not None:
            raise CameraSDKError(str(request["error"]))
        return request["frame"]

    def _fulfill_snapshot_requests(self, data_buf, frame_info) -> None:
        """用录像线程刚取得的一帧满足所有等待中的快照请求。"""
        with self._snapshot_condition:
            requests = list(self._snapshot_requests)
            self._snapshot_requests.clear()

        for request in requests:
            try:
                save_path = str(request["save_path"])
                self._save_frame(save_path, data_buf, frame_info)
                request["frame"] = FrameInfo(
                    width=int(getattr(frame_info, "nWidth", 0)),
                    height=int(getattr(frame_info, "nHeight", 0)),
                    frame_num=int(getattr(frame_info, "nFrameNum", 0)),
                    pixel_type=int(getattr(frame_info, "enPixelType", 0)),
                    frame_len=int(getattr(frame_info, "nFrameLen", 0)),
                    saved_path=save_path,
                    timestamp=time.time(),
                )
            except Exception as exc:
                request["error"] = exc
            finally:
                request["done"] = True

        if requests:
            with self._snapshot_condition:
                self._snapshot_condition.notify_all()

    def capture_once(self, save_path: str, timeout_ms: Optional[int] = None) -> FrameInfo:
        if self._record_snapshot_accepting:
            return self.capture_snapshot_during_recording(save_path, timeout_ms=timeout_ms)
        with self._sdk_lock:
            if not self._is_background_recording_unlocked() and not self._record_snapshot_accepting:
                return self._capture_once_unlocked(save_path, timeout_ms=timeout_ms)
        return self.capture_snapshot_during_recording(save_path, timeout_ms=timeout_ms)

    def _capture_once_unlocked(self, save_path: str, timeout_ms: Optional[int] = None) -> FrameInfo:
        """
        软件触发采一张图并保存。

        若当前正在后台录像，则不会额外抢占 SDK 取流；请求会交给录像线程，
        由下一帧数据完成快照保存。

        流程：
        1. 检查相机是否已打开
        2. 检查 payload_size 是否已获取
        3. 如未开始取流则启动取流
        4. 发送 TriggerSoftware 命令
        5. 调用 MV_CC_GetOneFrameTimeout 获取一帧原始数据
        6. 调用 _save_frame 校验 Mono8 并保存到磁盘
        7. 返回本帧信息
        """
        if not self.opened:
            raise CameraSDKError("相机尚未打开，请先调用 open()")
        if not self.payload_size:
            raise CameraSDKError("payload_size 未初始化")
        if not self.grabbing:
            self._start_grabbing_unlocked()

        timeout_ms = int(timeout_ms if timeout_ms is not None else self.grab_timeout_ms)
        save_path = str(Path(save_path))
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)

        data_buf, frame_info = self._grab_one_frame(timeout_ms)
        self._save_frame(save_path, data_buf, frame_info)

        return FrameInfo(
            width=int(getattr(frame_info, "nWidth", 0)),
            height=int(getattr(frame_info, "nHeight", 0)),
            frame_num=int(getattr(frame_info, "nFrameNum", 0)),
            pixel_type=int(getattr(frame_info, "enPixelType", 0)),
            frame_len=int(getattr(frame_info, "nFrameLen", 0)),
            saved_path=save_path,
            timestamp=time.time(),
        )

    def capture_bmp(self, save_path: str, timeout_ms: Optional[int] = None) -> FrameInfo:
        save_path = str(Path(save_path).with_suffix(".bmp"))
        return self.capture_once(save_path=save_path, timeout_ms=timeout_ms)

    def capture_jpg(self, save_path: str, timeout_ms: Optional[int] = None) -> FrameInfo:
        save_path = str(Path(save_path).with_suffix(".jpg"))
        return self.capture_once(save_path=save_path, timeout_ms=timeout_ms)

    def capture_png(self, save_path: str, timeout_ms: Optional[int] = None) -> FrameInfo:
        save_path = str(Path(save_path).with_suffix(".png"))
        return self.capture_once(save_path=save_path, timeout_ms=timeout_ms)

    def _resolve_record_frame_rate(self, fps: Optional[float]) -> float:
        if fps is not None and float(fps) > 0:
            return float(fps)
        for key in ("ResultingFrameRate", "AcquisitionFrameRate"):
            try:
                value = self._get_float_value(key)
                if value > 0:
                    return float(value)
            except Exception:
                continue
        return 20.0

    def start_recording(
        self,
        save_path: str,
        *,
        fps: Optional[float] = None,
        bitrate_kbps: int = 1000,
    ) -> None:
        with self._sdk_lock:
            self._start_recording_unlocked(save_path, fps=fps, bitrate_kbps=bitrate_kbps)

    def _start_recording_unlocked(
        self,
        save_path: str,
        *,
        fps: Optional[float] = None,
        bitrate_kbps: int = 1000,
    ) -> None:
        """启动 MVS SDK 录像会话；后续每帧需通过 _input_record_frame 写入。"""
        if not self.opened:
            raise CameraSDKError("相机尚未打开，请先调用 open()")
        if self.recording:
            raise CameraSDKError(f"录像已在进行中: {self._record_path}")
        if not hasattr(self.cam, "MV_CC_StartRecord"):
            raise CameraSDKError("当前 MVS Python 包装不支持 MV_CC_StartRecord")

        MV_CC_RECORD_PARAM = self._sdk.get("MV_CC_RECORD_PARAM")
        if MV_CC_RECORD_PARAM is None:
            raise CameraSDKError("当前 MVS Python 包装中不存在 MV_CC_RECORD_PARAM")

        save_path = str(Path(save_path).with_suffix(".avi"))
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)

        width = self._get_int_value("Width")
        height = self._get_int_value("Height")
        pixel_type = self._get_enum_value("PixelFormat")
        if not self._is_expected_pixel_type(pixel_type):
            raise CameraSDKError(
                f"recording PixelFormat mismatch before StartRecord: "
                f"expected {self.pixel_format}, actual pixel_type={pixel_type}"
            )
        frame_rate = self._resolve_record_frame_rate(fps)

        record_param = MV_CC_RECORD_PARAM()
        ctypes.memset(ctypes.byref(record_param), 0, ctypes.sizeof(record_param))
        record_param.nWidth = int(width)
        record_param.nHeight = int(height)
        record_param.enPixelType = int(pixel_type)
        record_param.fFrameRate = float(frame_rate)
        record_param.nBitRate = int(bitrate_kbps)
        record_param.enRecordFmtType = int(self._sdk.get("MV_FormatType_AVI", 1))
        record_param.strFilePath = str(save_path).encode("utf-8")

        try:
            self._configure_trigger_mode_unlocked(continuous=True, fps=frame_rate)
        except Exception:
            if self._sdk_hung:
                raise
            logger.warning(
                "failed to switch camera to continuous acquisition for recording; "
                "falling back to software trigger",
                exc_info=True,
            )

        try:
            self._timed_sdk_call(
                lambda: self._check(self.cam.MV_CC_StartRecord(record_param), "MV_CC_StartRecord"),
                _SDK_STOP_RECORD_TIMEOUT_S,
                "MV_CC_StartRecord",
            )
        except Exception:
            if self._record_continuous_mode:
                try:
                    self._configure_trigger_mode_unlocked(continuous=False)
                except Exception:
                    logger.exception("failed to restore software trigger after StartRecord failure")
            raise

        self.recording = True
        self._record_path = save_path
        self._record_started_at = time.time()
        self._record_width = int(width)
        self._record_height = int(height)
        self._record_pixel_type = int(pixel_type)
        self._record_frame_rate = float(frame_rate)
        self._record_bitrate_kbps = int(bitrate_kbps)
        self._record_frame_count = 0
        logger.info("recording started: path=%s fps=%s bitrate=%s", save_path, frame_rate, bitrate_kbps)

    def _input_record_frame(self, data_buf, frame_len: int) -> None:
        """把当前采集帧送入 MVS 录像编码器。"""
        self._ensure_sdk_healthy("MV_CC_InputOneFrame")
        if not self.recording:
            raise CameraSDKError("录像尚未开始")
        if not hasattr(self.cam, "MV_CC_InputOneFrame"):
            raise CameraSDKError("当前 MVS Python 包装不支持 MV_CC_InputOneFrame")

        MV_CC_INPUT_FRAME_INFO = self._sdk.get("MV_CC_INPUT_FRAME_INFO")
        if MV_CC_INPUT_FRAME_INFO is None:
            raise CameraSDKError("当前 MVS Python 包装中不存在 MV_CC_INPUT_FRAME_INFO")

        input_frame = MV_CC_INPUT_FRAME_INFO()
        ctypes.memset(ctypes.byref(input_frame), 0, ctypes.sizeof(input_frame))
        input_frame.pData = ctypes.cast(data_buf, ctypes.POINTER(ctypes.c_ubyte))
        input_frame.nDataLen = int(frame_len)

        ret = self.cam.MV_CC_InputOneFrame(input_frame)
        self._check(ret, "MV_CC_InputOneFrame")
        self._record_frame_count += 1

    def record_one_frame(self, timeout_ms: Optional[int] = None) -> FrameInfo:
        with self._sdk_lock:
            return self._record_one_frame_unlocked(timeout_ms=timeout_ms)

    def _grab_encode_record_frame_unlocked(self, timeout_ms: Optional[int] = None):
        """采集一帧并写入录像编码器；不处理快照，以便调用方把磁盘保存放到 SDK 锁外。"""
        if not self.opened:
            raise CameraSDKError("相机尚未打开，请先调用 open()")
        if not self.payload_size:
            raise CameraSDKError("payload_size 未初始化")
        if not self.grabbing:
            self._start_grabbing_unlocked()

        grab_timeout_ms = self._resolve_record_grab_timeout_ms(timeout_ms)
        data_buf, frame_info = self._grab_one_frame(
            grab_timeout_ms,
            send_software_trigger=not self._record_continuous_mode,
        )
        _, _, frame_len, _, _ = self._validate_mono8_frame_info(frame_info, context="record frame")
        self._input_record_frame(data_buf, frame_len)
        return data_buf, frame_info, frame_len

    def _record_one_frame_unlocked(self, timeout_ms: Optional[int] = None) -> FrameInfo:
        """采集一帧写入当前录像，并处理等待中的快照请求。"""
        data_buf, frame_info, frame_len = self._grab_encode_record_frame_unlocked(timeout_ms=timeout_ms)
        self._fulfill_snapshot_requests(data_buf, frame_info)

        return FrameInfo(
            width=int(getattr(frame_info, "nWidth", 0)),
            height=int(getattr(frame_info, "nHeight", 0)),
            frame_num=int(getattr(frame_info, "nFrameNum", 0)),
            pixel_type=int(getattr(frame_info, "enPixelType", 0)),
            frame_len=frame_len,
            saved_path=str(self._record_path or ""),
            timestamp=time.time(),
        )

    def stop_recording(self) -> VideoRecordInfo:
        with self._sdk_lock:
            return self._stop_recording_unlocked()

    def _stop_recording_unlocked(self) -> VideoRecordInfo:
        if not self.recording:
            raise CameraSDKError("录像尚未开始")
        if not hasattr(self.cam, "MV_CC_StopRecord"):
            raise CameraSDKError("当前 MVS Python 包装不支持 MV_CC_StopRecord")

        self._timed_sdk_call(
            lambda: self._check(self.cam.MV_CC_StopRecord(), "MV_CC_StopRecord"),
            _SDK_STOP_RECORD_TIMEOUT_S,
            "MV_CC_StopRecord",
        )

        finished_at = time.time()
        started_at = float(self._record_started_at or finished_at)
        info = VideoRecordInfo(
            saved_path=str(self._record_path or ""),
            width=int(self._record_width),
            height=int(self._record_height),
            pixel_type=int(self._record_pixel_type),
            frame_rate=float(self._record_frame_rate),
            bitrate_kbps=int(self._record_bitrate_kbps),
            frame_count=int(self._record_frame_count),
            duration_s=float(finished_at - started_at),
            timestamp_started=started_at,
            timestamp_finished=finished_at,
        )

        self.recording = False
        self._record_path = None
        self._record_started_at = None
        self._record_width = 0
        self._record_height = 0
        self._record_pixel_type = 0
        self._record_frame_rate = 0.0
        self._record_bitrate_kbps = 0
        self._record_frame_count = 0
        logger.info("recording stopped: %s", info)
        if self._record_continuous_mode:
            try:
                self._configure_trigger_mode_unlocked(continuous=False)
            except Exception:
                logger.exception("failed to restore software trigger after StopRecord")
        return info

    def record_video(
        self,
        save_path: str,
        *,
        duration_s: float,
        fps: Optional[float] = None,
        bitrate_kbps: int = 1000,
        timeout_ms: Optional[int] = None,
    ) -> VideoRecordInfo:
        with self._sdk_lock:
            return self._record_video_unlocked(
                save_path,
                duration_s=duration_s,
                fps=fps,
                bitrate_kbps=bitrate_kbps,
                timeout_ms=timeout_ms,
            )

    def _record_video_unlocked(
        self,
        save_path: str,
        *,
        duration_s: float,
        fps: Optional[float] = None,
        bitrate_kbps: int = 1000,
        timeout_ms: Optional[int] = None,
    ) -> VideoRecordInfo:
        """同步录像指定时长；调用期间会阻塞当前线程。"""
        if float(duration_s) <= 0:
            raise ValueError("duration_s 必须大于 0")

        self._record_grab_timeout_ms = self._resolve_record_grab_timeout_ms(timeout_ms)
        self._start_recording_unlocked(save_path, fps=fps, bitrate_kbps=bitrate_kbps)
        frame_interval = 1.0 / max(float(self._record_frame_rate), 0.001)
        deadline = time.monotonic() + float(duration_s)
        next_frame_at = time.monotonic()
        try:
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now < next_frame_at:
                    time.sleep(min(next_frame_at - now, 0.01))
                    continue
                self._record_one_frame_unlocked(timeout_ms=timeout_ms)
                next_frame_at += frame_interval
            return self._stop_recording_unlocked()
        except Exception:
            if self.recording:
                try:
                    self._stop_recording_unlocked()
                except Exception:
                    pass
            raise

    def _recording_worker(self, timeout_ms: Optional[int]) -> None:
        """后台录像线程：按目标帧率取帧、写入录像，并处理快照请求。"""
        frame_interval = 1.0 / max(float(self._record_frame_rate), 0.001)
        next_frame_at = time.monotonic()
        try:
            while not self._record_stop_event.is_set():
                now = time.monotonic()
                if now < next_frame_at:
                    self._record_stop_event.wait(min(next_frame_at - now, 0.01))
                    continue
                data_buf = None
                frame_info = None
                with self._sdk_lock:
                    if self._record_stop_event.is_set():
                        break
                    data_buf, frame_info, _frame_len = self._grab_encode_record_frame_unlocked(
                        timeout_ms=timeout_ms
                    )
                if data_buf is not None and frame_info is not None:
                    self._fulfill_snapshot_requests(data_buf, frame_info)
                next_frame_at += frame_interval
                now = time.monotonic()
                if next_frame_at < now - frame_interval:
                    # Drop missed scheduling slots and provide a real wait on
                    # the next iteration.  Setting the deadline to now causes
                    # an immediate lock reacquire and can starve control calls.
                    next_frame_at = now + frame_interval
        except Exception as exc:
            self._record_error = str(exc)
            logger.exception("background recording failed")
        finally:
            with self._snapshot_condition:
                self._record_snapshot_accepting = False
                self._fail_pending_snapshot_requests_unlocked(
                    self._record_error or "background recording stopped before snapshot was captured"
                )

    def start_background_recording(
        self,
        save_path: str,
        *,
        fps: Optional[float] = None,
        bitrate_kbps: int = 1000,
        timeout_ms: Optional[int] = None,
    ) -> None:
        with self._sdk_lock:
            self._start_background_recording_unlocked(
                save_path,
                fps=fps,
                bitrate_kbps=bitrate_kbps,
                timeout_ms=timeout_ms,
            )

    def _start_background_recording_unlocked(
        self,
        save_path: str,
        *,
        fps: Optional[float] = None,
        bitrate_kbps: int = 1000,
        timeout_ms: Optional[int] = None,
    ) -> None:
        """启动后台录像线程；调用方可继续执行拍照或流程调度。"""
        self._record_error = None
        self._record_stop_event.clear()
        self._record_grab_timeout_ms = self._resolve_record_grab_timeout_ms(timeout_ms)
        self._start_recording_unlocked(save_path, fps=fps, bitrate_kbps=bitrate_kbps)
        self._record_thread = threading.Thread(
            target=self._recording_worker,
            args=(timeout_ms,),
            name="hik-camera-recording",
            daemon=True,
        )
        with self._snapshot_condition:
            self._record_snapshot_accepting = True
        try:
            self._record_thread.start()
        except Exception:
            with self._snapshot_condition:
                self._record_snapshot_accepting = False
            raise

    def stop_background_recording(self, join_timeout_s: float = 5.0) -> VideoRecordInfo:
        """请求后台录像线程停止，并返回 MVS 录像结果。

        必须先无锁设置停止事件再 join。若为了读线程引用而先拿 `_sdk_lock`，
        录像线程卡在原生 SDK 调用时 stop 会一起被堵死。
        """
        self._record_stop_event.set()
        with self._snapshot_condition:
            self._record_snapshot_accepting = False
            self._fail_pending_snapshot_requests_unlocked(
                "background recording is stopping before snapshot was captured"
            )
        thread = self._record_thread
        join_timeout_s = self._join_timeout_for_recording_stop(join_timeout_s)
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=join_timeout_s)
            if thread.is_alive():
                message = f"后台录像线程未在 {join_timeout_s}s 内退出，已拒绝停止 MVS 录像以避免并发写帧"
                self._record_error = message
                self._fail_pending_snapshot_requests(message)
                raise CameraSDKError(message)

        if self._sdk_hung:
            message = "停止录像时 SDK 已挂起，已拒绝继续调用原生接口"
            self._record_error = message
            raise CameraSDKError(message)
        acquired = self._sdk_lock.acquire(timeout=_SDK_LOCK_STOP_ACQUIRE_TIMEOUT_S)
        if not acquired:
            message = "停止录像时无法获取 SDK 锁，相机调用可能已挂起"
            self._record_error = message
            raise CameraSDKError(message)
        try:
            if self._record_thread is thread:
                self._record_thread = None
            self._fail_pending_snapshot_requests("background recording stopped before snapshot was captured")
            info = self._stop_recording_unlocked()
            background_error = self._record_error
            if background_error:
                raise CameraSDKError(
                    f"background recording failed before stop completed: {background_error}"
                )
            return info
        finally:
            self._sdk_lock.release()

    def recording_status(self) -> Dict[str, Any]:
        # 不得等待 _sdk_lock：录像线程会在取帧/编码期间长时间持有该锁。
        now = time.time()
        started_at = self._record_started_at
        thread = self._record_thread
        return {
            "recording": bool(self.recording),
            "background": bool(self.recording and thread is not None and thread.is_alive()),
            "saved_path": self._record_path,
            "frame_rate": self._record_frame_rate,
            "bitrate_kbps": self._record_bitrate_kbps,
            "frame_count": self._record_frame_count,
            "duration_s": float(now - started_at) if started_at else 0.0,
            "error": self._record_error or ("SDK hung" if self._sdk_hung else None),
            "sdk_hung": bool(self._sdk_hung),
            "opened": bool(self.opened),
        }

    def close(self) -> bool:
        if self._closed:
            return True
        if self._sdk_hung:
            logger.error("camera close skipped: previous MVS SDK call is still hung")
            return False
        thread = self._record_thread
        background_recording = bool(self.recording and thread is not None and thread.is_alive())
        if background_recording:
            try:
                self.stop_background_recording()
            except Exception as exc:
                logger.error("background recording did not stop cleanly; skip camera close to avoid SDK race: %s", exc)
                return False
        acquired = self._sdk_lock.acquire(timeout=_SDK_LOCK_STOP_ACQUIRE_TIMEOUT_S)
        if not acquired:
            logger.error("camera close skipped: SDK lock still held, process may need restart")
            return False
        try:
            if self._is_background_recording_unlocked():
                logger.error("background recording is still active; skip camera close to avoid SDK race")
                return False
            return self._close_unlocked()
        finally:
            self._sdk_lock.release()

    def _close_unlocked(self) -> bool:
        """
        关闭相机并释放资源。

        释放顺序：
        1. 如果正在录像，先尝试停止录像
        2. 停止取流
        3. 关闭设备
        4. 销毁句柄
        5. 释放本实例持有的 SDK 生命周期引用；最后一个引用释放时才执行 MV_CC_Finalize

        原生调用带硬超时。超时或 SDK 已挂起时不清理 Python 侧句柄，避免误报关闭成功。
        """
        if not self._sdk_loaded:
            self._closed = True
            return True
        if self._sdk_hung:
            return False
        try:
            if self.cam is not None:
                if self.recording:
                    self._stop_recording_unlocked()
                if self.grabbing:
                    self._stop_grabbing_unlocked()
                if self._device_opened:
                    self._timed_sdk_call(
                        lambda: self._check(self.cam.MV_CC_CloseDevice(), "MV_CC_CloseDevice"),
                        _SDK_CLOSE_TIMEOUT_S,
                        "MV_CC_CloseDevice",
                    )
                    self._device_opened = False
                if self._handle_created:
                    self._timed_sdk_call(
                        lambda: self._check(self.cam.MV_CC_DestroyHandle(), "MV_CC_DestroyHandle"),
                        _SDK_CLOSE_TIMEOUT_S,
                        "MV_CC_DestroyHandle",
                    )
                    self._handle_created = False
        except Exception:
            logger.exception("camera close native call failed or timed out; keeping handle")
            return False

        if self._sdk_initialized:
            finalized_cleanly = _release_mvs_sdk(self._sdk["MvCamera"])
            self._sdk_initialized = False
            if not finalized_cleanly:
                self._sdk_hung = True
                self._record_error = "MVS SDK finalization failed; worker process must be replaced"
                return False
        self.opened = False
        self.grabbing = False
        self.recording = False
        self._record_continuous_mode = False
        self._record_snapshot_accepting = False
        self._handle_created = False
        self._device_opened = False
        self.cam = None
        self.payload_size = None
        self.device_info = None
        self._grab_buffer = None
        self._grab_buffer_size = 0
        self._closed = True
        return True

    def _save_frame(self, save_path: str, data_buf, frame_info) -> None:
        """
        保存一帧图片。

        当前生产路径只接受 Mono8。若实际帧 PixelFormat 或字节长度不符合预期，
        会先保存同名 .raw 文件再抛出异常，便于现场排查相机输出格式问题。
        """
        width = int(getattr(frame_info, "nWidth", 0))
        height = int(getattr(frame_info, "nHeight", 0))
        frame_len = int(getattr(frame_info, "nFrameLen", 0))
        pixel_type = int(getattr(frame_info, "enPixelType", 0))

        logger.info(
            "save frame: width=%s height=%s frame_len=%s pixel_type=%s path=%s",
            width, height, frame_len, pixel_type, save_path
        )

        raw_len = max(frame_len, 0)
        raw = np.ctypeslib.as_array(data_buf, shape=(raw_len,))

        try:
            self._validate_mono8_frame_info(frame_info, context="save frame")
        except CameraSDKError as exc:
            raw_path = self._dump_raw_frame(save_path, raw, raw_len)
            raise CameraSDKError(f"{exc}; raw frame saved to {raw_path}") from exc

        # 到这里说明帧已经通过 Mono8 和长度校验，可以按 8-bit 灰度图保存。
        img = raw[: width * height].reshape((height, width)).copy()
        im = Image.fromarray(img, mode="L")
        im.save(save_path)
        logger.info("saved mono8 image by Pillow: %s", save_path)
        return

    def _dump_raw_frame(self, save_path: str, raw: np.ndarray, frame_len: int) -> str:
        raw_path = str(Path(save_path).with_suffix(".raw"))
        with open(raw_path, "wb") as f:
            f.write(bytes(raw[:frame_len]))
        return raw_path

    def _infer_image_type(self, ext: str) -> int:
        """旧版 SDK SaveImageEx2 路径使用的格式映射；当前 Pillow 保存路径未调用。"""
        if ext in (".jpg", ".jpeg"):
            return int(self._sdk.get("MV_Image_Jpeg", 1))
        if ext == ".png":
            return int(self._sdk.get("MV_Image_Png", 3))
        return int(self._sdk.get("MV_Image_Bmp", 0))



def build_camera(
    mvs_python_dir: Optional[str] = None,
    device_index: int = 0,
    serial_number: Optional[str] = None,
    camera_ip: Optional[str] = None,
    pixel_format: str = "mono8",
    exposure_us: Optional[float] = None,
    gain: Optional[float] = None,
) -> HikCameraController:
    return HikCameraController(
        mvs_python_dir=mvs_python_dir,
        device_index=device_index,
        serial_number=serial_number,
        camera_ip=camera_ip,
        pixel_format=pixel_format,
        default_exposure_us=exposure_us,
        default_gain=gain,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hikvision camera single-shot tester")
    parser.add_argument("--mvs-python-dir", default=DEFAULT_MVS_PYTHON_DIR)
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--serial-number", default=None)
    parser.add_argument("--camera-ip", default=None)
    parser.add_argument("--pixel-format", default="mono8")
    parser.add_argument("--exposure-us", type=float, default=None)
    parser.add_argument("--gain", type=float, default=None)
    parser.add_argument("--save-path", default="test_capture.bmp")
    parser.add_argument("--record-path", default=None)
    parser.add_argument("--record-duration-s", type=float, default=0.0)
    parser.add_argument("--record-fps", type=float, default=None)
    parser.add_argument("--record-bitrate-kbps", type=int, default=1000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")

    cam = HikCameraController(
        mvs_python_dir=args.mvs_python_dir,
        device_index=args.device_index,
        serial_number=args.serial_number,
        camera_ip=args.camera_ip,
        pixel_format=args.pixel_format,
        default_exposure_us=args.exposure_us,
        default_gain=args.gain,
    )
    try:
        cam.open()
        try:
            if args.exposure_us is not None:
                print(f"current exposure_us={cam.get_exposure_us():.3f}")
        except Exception as e:
            print(f"read exposure failed: {e}")
        try:
            if args.gain is not None:
                print(f"current gain={cam.get_gain():.3f}")
        except Exception as e:
            print(f"read gain failed: {e}")
        if args.record_path:
            video = cam.record_video(
                args.record_path,
                duration_s=args.record_duration_s or 5.0,
                fps=args.record_fps,
                bitrate_kbps=args.record_bitrate_kbps,
            )
            print(video)
        else:
            frame = cam.capture_once(args.save_path)
            print(frame)
    finally:
        cam.close()
