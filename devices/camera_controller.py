from __future__ import annotations
"""
camera_controller.py

功能：
1. 封装海康工业相机 MVS Python SDK 的常用调用流程；
2. 支持按设备索引或序列号选择相机；
3. 支持软件触发单帧采图；
4. 支持设置曝光、增益；
5. 支持将 Mono8 图像保存为 bmp/jpg/png。

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
# import cv2  # 如后续需要 OpenCV 保存/处理，可再启用
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
DEFAULT_MVS_PYTHON_DIR = r"C:\Program Files (x86)\MVS\Development\Samples\Python\MvImport"


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


class HikCameraController:
    """
    海康工业相机控制器。

    设计目标：
    - 让上层代码只关心“打开相机 / 设置参数 / 采图 / 关闭相机”
    - 将 SDK 的导入、设备枚举、句柄创建、异常处理都收敛在这里

    目录建议：
        project_root/
        ├── devices/
        │   └── camera_controller.py
        ├── config/
        └── services/

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
        trigger_source: str = "software",
        grab_timeout_ms: int = 1500,
        jpg_quality: int = 90,
        default_exposure_us: Optional[float] = None,
        default_gain: Optional[float] = None,
    ) -> None:
        # MVS Python 模块目录
        self.mvs_python_dir = mvs_python_dir or os.getenv("MVS_PYTHON_DIR") or DEFAULT_MVS_PYTHON_DIR
        # 相机选择方式：
        # - 若 serial_number 不为空，则优先按序列号匹配
        # - 否则按 device_index 选择
        self.device_index = device_index
        self.serial_number = serial_number
        # 当前仅实现 software 软件触发
        self.trigger_source = trigger_source.lower().strip()
        # 获取单帧时的等待超时，单位 ms
        self.grab_timeout_ms = int(grab_timeout_ms)
        # jpg 保存质量，仅在保存 jpg 时有意义
        self.jpg_quality = int(jpg_quality)
        # 打开相机后若提供默认曝光 / 增益，则自动设置
        self.default_exposure_us = default_exposure_us
        self.default_gain = default_gain

        # SDK 是否已完成导入
        self._sdk_loaded = False

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
                "2) 目录中存在 MvCameraControl_class.py；\n"
                "3) 该目录已通过构造参数或环境变量 MVS_PYTHON_DIR 传入。"
            ) from e

        consts: Dict[str, Any] = {}
        for name in dir(const_mod):
            if name.startswith("MV_"):
                consts[name] = getattr(const_mod, name)

        sdk = {
            "MvCamera": getattr(mv_mod, "MvCamera"),
            "MV_CC_DEVICE_INFO_LIST": getattr(hdr_mod, "MV_CC_DEVICE_INFO_LIST"),
            "MV_CC_DEVICE_INFO": getattr(hdr_mod, "MV_CC_DEVICE_INFO"),
            "MVCC_INTVALUE": getattr(hdr_mod, "MVCC_INTVALUE", None),
            "MVCC_ENUMVALUE": getattr(hdr_mod, "MVCC_ENUMVALUE", None),
            "MVCC_FLOATVALUE": getattr(hdr_mod, "MVCC_FLOATVALUE", None),
            "MV_FRAME_OUT_INFO_EX": getattr(hdr_mod, "MV_FRAME_OUT_INFO_EX"),
            "MV_SAVE_IMAGE_PARAM_EX": getattr(hdr_mod, "MV_SAVE_IMAGE_PARAM_EX"),
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

    def _call_variants(self, func, variants, func_name: str):
        last_exc = None
        for args in variants:
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

        ret = self._call_variants(
            MvCamera.MV_CC_EnumDevices,
            [
                (device_mask, dev_list),
                (device_mask, ctypes.byref(dev_list)),
            ],
            "MV_CC_EnumDevices",
        )
        self._check(ret, "MV_CC_EnumDevices")
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

    def _select_device(self, dev_list):
        n = int(getattr(dev_list, "nDeviceNum", 0))
        if n <= 0:
            raise CameraSDKError("未枚举到相机设备")

        MV_CC_DEVICE_INFO = self._sdk["MV_CC_DEVICE_INFO"]
        matched = None
        for i in range(n):
            ptr = dev_list.pDeviceInfo[i]
            dev_info = ctypes.cast(ptr, ctypes.POINTER(MV_CC_DEVICE_INFO)).contents
            serial = self._get_device_serial(dev_info)
            logger.info("camera device[%s] serial=%s", i, serial or "<unknown>")
            if self.serial_number and serial == self.serial_number:
                matched = dev_info
                break

        if matched is not None:
            return matched
        if self.serial_number:
            raise CameraSDKError(f"未找到序列号为 {self.serial_number} 的相机")
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
        """
        打开相机并完成基础初始化。

        执行顺序：
        1. 导入 SDK
        2. 创建 MvCamera 实例
        3. 初始化 SDK 全局环境
        4. 枚举并选择设备
        5. 创建句柄
        6. 打开设备
        7. 配置网络包大小（GigE 可优化）
        8. 设置触发模式
        9. 设置默认曝光、增益
        10. 开始取流
        11. 读取 PayloadSize
        """
        if self.opened:
            return

        self._load_sdk()
        MvCamera = self._sdk["MvCamera"]
        self.cam = MvCamera()

        ret = MvCamera.MV_CC_Initialize()
        self._check(ret, "MV_CC_Initialize")

        dev_list = self._enum_devices()
        self.device_info = self._select_device(dev_list)

        ret = self._call_variants(
            self.cam.MV_CC_CreateHandle,
            [
                (self.device_info,),
                (ctypes.byref(self.device_info),),
            ],
            "MV_CC_CreateHandle",
        )
        self._check(ret, "MV_CC_CreateHandle")

        access_exclusive = int(self._sdk.get("MV_ACCESS_Exclusive", 1))
        ret = self._call_variants(
            self.cam.MV_CC_OpenDevice,
            [
                (access_exclusive, 0),
                (),
            ],
            "MV_CC_OpenDevice",
        )
        self._check(ret, "MV_CC_OpenDevice")

        self._try_set_optimal_packet_size()
        self._set_trigger_mode()

        if self.default_exposure_us is not None:
            self.set_exposure_us(self.default_exposure_us)
        if self.default_gain is not None:
            self.set_gain(self.default_gain)

        self.start_grabbing()
        self.payload_size = self._get_int_value("PayloadSize")
        if self.payload_size <= 0:
            raise CameraSDKError("PayloadSize 获取失败")

        self.opened = True
        logger.info("camera opened, payload_size=%s", self.payload_size)

    def _try_set_optimal_packet_size(self) -> None:
        try:
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

    def start_grabbing(self) -> None:
        """开始取流。"""
        if self.grabbing:
            return
        ret = self.cam.MV_CC_StartGrabbing()
        self._check(ret, "MV_CC_StartGrabbing")
        self.grabbing = True

    def stop_grabbing(self) -> None:
        """停止取流。"""
        if self.cam is None or not self.grabbing:
            return
        try:
            ret = self.cam.MV_CC_StopGrabbing()
            self._check(ret, "MV_CC_StopGrabbing")
        finally:
            self.grabbing = False

    def set_exposure_us(self, exposure_us: float) -> None:
        """设置曝光时间，单位微秒。会先关闭自动曝光。"""
        try:
            self._set_enum("ExposureAuto", int(self._sdk.get("MV_EXPOSURE_AUTO_MODE_OFF", 0)))
        except Exception:
            try:
                self._set_enum("ExposureMode", int(self._sdk.get("MV_EXPOSURE_MODE_TIMED", 0)))
            except Exception:
                pass
        self._set_float("ExposureTime", float(exposure_us))
        logger.info("set exposure_us=%s", exposure_us)

    def get_exposure_us(self) -> float:
        return self._get_float_value("ExposureTime")

    def set_gain(self, gain: float) -> None:
        """设置增益，单位通常为 dB。会先关闭自动增益。"""
        try:
            self._set_enum("GainAuto", int(self._sdk.get("MV_GAIN_MODE_OFF", 0)))
        except Exception:
            pass
        self._set_float("Gain", float(gain))
        logger.info("set gain=%s", gain)

    def get_gain(self) -> float:
        return self._get_float_value("Gain")

    def capture_once(self, save_path: str, timeout_ms: Optional[int] = None) -> FrameInfo:
        """
        软件触发采一张图并保存。

        流程：
        1. 检查相机是否已打开
        2. 检查 payload_size 是否已获取
        3. 如未开始取流则启动取流
        4. 发送 TriggerSoftware 命令
        5. 调用 MV_CC_GetOneFrameTimeout 获取一帧原始数据
        6. 调用 _save_frame 保存到磁盘
        7. 返回本帧信息
        """
        if not self.opened:
            raise CameraSDKError("相机尚未打开，请先调用 open()")
        if not self.payload_size:
            raise CameraSDKError("payload_size 未初始化")
        if not self.grabbing:
            self.start_grabbing()

        timeout_ms = int(timeout_ms if timeout_ms is not None else self.grab_timeout_ms)
        save_path = str(Path(save_path))
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)

        self._set_command("TriggerSoftware")

        MV_FRAME_OUT_INFO_EX = self._sdk["MV_FRAME_OUT_INFO_EX"]
        frame_info = MV_FRAME_OUT_INFO_EX()
        ctypes.memset(ctypes.byref(frame_info), 0, ctypes.sizeof(frame_info))

        data_buf = (ctypes.c_ubyte * int(self.payload_size))()
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

    def close(self) -> None:
        """
        关闭相机并释放资源。

        注意：
        - 先停流
        - 再关闭设备
        - 再销毁句柄
        - 最后全局 Finalize
        """
        if not self._sdk_loaded:
            return
        try:
            if self.cam is not None:
                try:
                    self.stop_grabbing()
                except Exception:
                    pass
                try:
                    self.cam.MV_CC_CloseDevice()
                except Exception:
                    pass
                try:
                    self.cam.MV_CC_DestroyHandle()
                except Exception:
                    pass
        finally:
            try:
                self._sdk["MvCamera"].MV_CC_Finalize()
            except Exception:
                pass
            self.opened = False
            self.grabbing = False
            self.cam = None
            self.payload_size = None

    # def _save_frame(self, save_path: str, data_buf, frame_info) -> None:
    #     ext = Path(save_path).suffix.lower()
    #     image_type = self._infer_image_type(ext)

    #     MV_SAVE_IMAGE_PARAM_EX = self._sdk["MV_SAVE_IMAGE_PARAM_EX"]
    #     save_param = MV_SAVE_IMAGE_PARAM_EX()
    #     ctypes.memset(ctypes.byref(save_param), 0, ctypes.sizeof(save_param))

    #     width = int(getattr(frame_info, "nWidth", 0))
    #     height = int(getattr(frame_info, "nHeight", 0))
    #     frame_len = int(getattr(frame_info, "nFrameLen", 0))
    #     pixel_type = int(getattr(frame_info, "enPixelType", 0))
    #     if width <= 0 or height <= 0 or frame_len <= 0:
    #         raise CameraSDKError("帧信息异常，无法保存图像")

    #     out_buf_size = max(width * height * 4 + 4096, frame_len * 2 + 4096)
    #     out_buf = (ctypes.c_ubyte * out_buf_size)()

    #     save_param.enImageType = image_type
    #     save_param.enPixelType = pixel_type
    #     save_param.nWidth = width
    #     save_param.nHeight = height
    #     save_param.nDataLen = frame_len
    #     save_param.pData = ctypes.cast(data_buf, ctypes.POINTER(ctypes.c_ubyte))
    #     save_param.pImageBuffer = ctypes.cast(out_buf, ctypes.POINTER(ctypes.c_ubyte))
    #     save_param.nBufferSize = out_buf_size
    #     if hasattr(save_param, "nJpgQuality"):
    #         save_param.nJpgQuality = int(self.jpg_quality)
    #     if hasattr(save_param, "iMethodValue"):
    #         save_param.iMethodValue = 0

    #     ret = self._call_variants(
    #         self.cam.MV_CC_SaveImageEx2,
    #         [
    #             (save_param,),
    #             (ctypes.byref(save_param),),
    #         ],
    #         "MV_CC_SaveImageEx2",
    #     )
    #     self._check(ret, "MV_CC_SaveImageEx2")

    #     out_len = int(getattr(save_param, "nImageLen", 0))
    #     if out_len <= 0:
    #         raise CameraSDKError("图像转换成功但输出长度为 0")

    #     with open(save_path, "wb") as f:
    #         f.write(bytes(out_buf[:out_len]))
    
    ###cv###
    # def _save_frame(self, save_path: str, data_buf, frame_info) -> None:
    #     width = int(getattr(frame_info, "nWidth", 0))
    #     height = int(getattr(frame_info, "nHeight", 0))
    #     frame_len = int(getattr(frame_info, "nFrameLen", 0))
    #     pixel_type = int(getattr(frame_info, "enPixelType", 0))

    #     if width <= 0 or height <= 0 or frame_len <= 0:
    #         raise CameraSDKError("帧信息异常，无法保存图像")

    #     # 先打印关键信息，便于排查
    #     logger.info(
    #         "save frame: width=%s height=%s frame_len=%s pixel_type=%s path=%s",
    #         width, height, frame_len, pixel_type, save_path
    #     )

    #     raw = np.ctypeslib.as_array(data_buf, shape=(frame_len,))

    #     ext = Path(save_path).suffix.lower()

    #     # 优先按 Mono8 直接保存：frame_len == width * height
    #     if frame_len == width * height:
    #         img = raw[: width * height].reshape((height, width)).copy()
    #         ok = cv2.imwrite(str(save_path), img)
    #         if not ok:
    #             raise CameraSDKError(f"cv2.imwrite 保存失败: {save_path}")
    #         logger.info("saved mono8 image by OpenCV: %s", save_path)
    #         return

    #     # 如果不是单通道 8bit，先把原始数据落盘，便于后续分析
    #     raw_path = str(Path(save_path).with_suffix(".raw"))
    #     with open(raw_path, "wb") as f:
    #         f.write(bytes(raw[:frame_len]))
    #     raise CameraSDKError(
    #         f"当前帧不是 width*height 的 Mono8 格式，已保存原始数据到: {raw_path} ; "
    #         f"width={width}, height={height}, frame_len={frame_len}, pixel_type={pixel_type}"
    #     )
    def _save_frame(self, save_path: str, data_buf, frame_info) -> None:
        width = int(getattr(frame_info, "nWidth", 0))
        height = int(getattr(frame_info, "nHeight", 0))
        frame_len = int(getattr(frame_info, "nFrameLen", 0))
        pixel_type = int(getattr(frame_info, "enPixelType", 0))

        if width <= 0 or height <= 0 or frame_len <= 0:
            raise CameraSDKError("帧信息异常，无法保存图像")

        logger.info(
            "save frame: width=%s height=%s frame_len=%s pixel_type=%s path=%s",
            width, height, frame_len, pixel_type, save_path
        )

        raw = np.ctypeslib.as_array(data_buf, shape=(frame_len,))

        # 先按 Mono8 处理：如果帧长度正好等于 width * height，
        # 就认为是一张 8bit 灰度图
        if frame_len == width * height:
            img = raw[: width * height].reshape((height, width)).copy()
            im = Image.fromarray(img, mode="L")
            im.save(save_path)
            logger.info("saved mono8 image by Pillow: %s", save_path)
            return

        # 否则先把原始数据落盘，便于后续继续分析像素格式
        raw_path = str(Path(save_path).with_suffix(".raw"))
        with open(raw_path, "wb") as f:
            f.write(bytes(raw[:frame_len]))

        raise CameraSDKError(
            f"当前帧不是 width*height 的 Mono8 格式，已保存原始数据到: {raw_path} ; "
            f"width={width}, height={height}, frame_len={frame_len}, pixel_type={pixel_type}"
        )
    def _infer_image_type(self, ext: str) -> int:
        if ext in (".jpg", ".jpeg"):
            return int(self._sdk.get("MV_Image_Jpeg", 1))
        if ext == ".png":
            return int(self._sdk.get("MV_Image_Png", 3))
        return int(self._sdk.get("MV_Image_Bmp", 0))



def build_camera(
    mvs_python_dir: Optional[str] = None,
    device_index: int = 0,
    serial_number: Optional[str] = None,
    exposure_us: Optional[float] = None,
    gain: Optional[float] = None,
) -> HikCameraController:
    return HikCameraController(
        mvs_python_dir=mvs_python_dir,
        device_index=device_index,
        serial_number=serial_number,
        default_exposure_us=exposure_us,
        default_gain=gain,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hikvision camera single-shot tester")
    parser.add_argument("--mvs-python-dir", default=DEFAULT_MVS_PYTHON_DIR)
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--serial-number", default=None)
    parser.add_argument("--exposure-us", type=float, default=None)
    parser.add_argument("--gain", type=float, default=None)
    parser.add_argument("--save-path", default="test_capture.bmp")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")

    cam = HikCameraController(
        mvs_python_dir=args.mvs_python_dir,
        device_index=args.device_index,
        serial_number=args.serial_number,
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
        frame = cam.capture_once(args.save_path)
        print(frame)
    finally:
        cam.close()
