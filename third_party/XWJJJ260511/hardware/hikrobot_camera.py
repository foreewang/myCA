"""海康机器人（Hikrobot）工业相机实时采图接口。"""

# 允许类型标注延迟解析。
from __future__ import annotations

# importlib 用于按字符串动态导入海康 MVS Python SDK。
import importlib
# logging 用于输出“相机打开成功、首帧取图成功”等诊断信息。
import logging
# os 用于读取 MVS SDK 环境变量。
import os
# socket 用于在没指定 net_export_ip 时推断本机出口 IP。
import socket
# sys 用于临时把 MvImport 目录加入 Python 搜索路径。
import sys
# ctypes 相关函数用于和海康 MVS C SDK 的结构体/指针交互。
from ctypes import POINTER, byref, c_ubyte, cast, memset, sizeof
# Path 用于处理 SDK 路径。
from pathlib import Path
# Optional 用于标注可以为空的配置项。
from typing import Optional

# OpenCV 用于 OpenCV 后端取图和 RGB/BGR 转换。
import cv2
# numpy 用于把 MVS 原始缓冲区转换成图像数组。
import numpy as np

# CameraBase 是项目统一相机接口。
from .camera import CameraBase

# 当前模块日志对象。
logger = logging.getLogger(__name__)


class HikrobotCamera(CameraBase):
    """
    海康机器人相机：实时采图，每次 capture() 返回当前一帧 BGR。
    支持通过 MVS SDK 或 OpenCV 两种方式（见下方实现）。
    """

    def __init__(
        self,
        device: Optional[str] = None,
        use_mvs: bool = True,
        opencv_index: int = 0,
        net_export_ip: Optional[str] = None,
        mvs_sdk_path: Optional[str] = None,
        exposure_auto: Optional[bool] = None,
        exposure_time_us: Optional[float] = None,
    ):
        """
        device: MVS 模式下为相机 IP，例如 192.168.1.253；OpenCV 模式下忽略。
        use_mvs: True 时尝试用 MVS 采图；False 或 MVS 不可用时用 OpenCV。
        opencv_index: use_mvs=False 时，OpenCV 打开的相机索引（0 为默认摄像头）。
        net_export_ip: 连接相机的电脑网卡 IP，例如 192.168.1.168。
        mvs_sdk_path: MVS Python MvImport 目录。
        exposure_auto: 是否启用相机自动曝光；None 表示不改相机当前设置。
        exposure_time_us: 手动曝光时间，单位微秒；None 表示不改相机当前设置。
        """
        if exposure_auto is True and exposure_time_us is not None:
            raise ValueError("exposure_auto=True 时不能同时设置 exposure_time_us")

        self._device = device
        # 保存是否使用 MVS；False 时走 OpenCV。
        self._use_mvs = use_mvs
        # 保存 OpenCV 相机索引。
        self._opencv_index = opencv_index
        # 保存连接相机的电脑网卡 IP。
        self._net_export_ip = net_export_ip
        # 保存 MVS Python SDK 路径。
        self._mvs_sdk_path = mvs_sdk_path
        # 保存曝光配置；None 表示沿用相机当前状态。
        self._exposure_auto = exposure_auto
        self._exposure_time_us = exposure_time_us
        # OpenCV VideoCapture 对象，只有 OpenCV 模式会用到。
        self._cap: Optional[cv2.VideoCapture] = None
        # MVS 相机句柄，打开成功后才会有值。
        self._mvs_handle = None  # 预留：MVS 相机句柄
        # MVS SDK 模块对象。
        self._mvs = None
        # 标记是否调用过 MVS 初始化。
        self._mvs_initialized = False
        # 标记是否正在采流。
        self._mvs_grabbing = False
        # 取帧计数，用于只在首帧打印一次日志。
        self._capture_count = 0
        # 初始化完成后立刻打开相机。
        self._open()

    def _open(self) -> None:
        # MVS 模式下必须成功打开 MVS，相机失败就直接报错。
        if self._use_mvs:
            try:
                self._open_mvs()
                return
            except Exception as exc:
                # 打开失败时先释放已初始化的资源。
                self.close()
                raise RuntimeError(
                    f"MVS 打开相机失败，ip={self._device}, net_export_ip={self._net_export_ip}"
                ) from exc
        # 非 MVS 模式使用 OpenCV 打开摄像头。
        self._open_opencv()

    def _open_mvs(self) -> None:
        """使用海康 MVS SDK 打开设备并启动采流。"""

        # 加载海康 MVS Python SDK。
        mvs = self._load_mvs_module()
        # 保存 SDK 模块，后续取图和关闭都要用。
        self._mvs = mvs
        # 初始化 MVS SDK。
        mvs.MvCamera.MV_CC_Initialize()
        self._mvs_initialized = True

        # 根据配置构造 MVS 设备信息。
        st_device_info = self._make_mvs_device_info(mvs)
        # 创建相机对象。
        cam = mvs.MvCamera()
        # 用设备信息创建 SDK 句柄。
        ret = cam.MV_CC_CreateHandle(st_device_info)
        self._check_mvs(ret, "create handle")

        try:
            # 独占方式打开相机；如果 MVS Viewer 正在占用，可能会失败。
            ret = cam.MV_CC_OpenDevice(mvs.MV_ACCESS_Exclusive, 0)
            self._check_mvs(ret, "open device")

            # GigE 相机可以设置最佳包大小，提高传输稳定性。
            if st_device_info.nTLayerType in {mvs.MV_GIGE_DEVICE, getattr(mvs, "MV_GENTL_GIGE_DEVICE", -1)}:
                packet_size = cam.MV_CC_GetOptimalPacketSize()
                if int(packet_size) > 0:
                    cam.MV_CC_SetIntValue("GevSCPSPacketSize", packet_size)

            # 关闭触发模式，使用连续采集。
            cam.MV_CC_SetEnumValue("TriggerMode", mvs.MV_TRIGGER_MODE_OFF)
            # 按配置设置曝光；未配置时不主动改相机当前状态。
            self._apply_mvs_exposure(cam)

            # 开始采流，后续 capture() 才能取到图像。
            ret = cam.MV_CC_StartGrabbing()
            self._check_mvs(ret, "start grabbing")
            # 保存相机句柄。
            self._mvs_handle = cam
            # 标记已经开始采流。
            self._mvs_grabbing = True
            logger.info("MVS 相机已打开: ip=%s, net_export_ip=%s", self._device, self._net_export_ip)
        except Exception:
            # 如果打开过程中失败，销毁句柄，避免资源泄漏。
            cam.MV_CC_CloseDevice()
            cam.MV_CC_DestroyHandle()
            raise

    def _open_opencv(self) -> None:
        """无 MVS 时用 OpenCV 打开相机（USB 或系统默认摄像头），便于联调。"""
        # 按索引打开 OpenCV 摄像头。
        self._cap = cv2.VideoCapture(self._opencv_index)
        # 打不开时直接报错。
        if not self._cap.isOpened():
            raise RuntimeError(f"OpenCV 无法打开相机 index={self._opencv_index}")
        self._apply_opencv_exposure()
        logger.info("OpenCV 相机已打开: index=%s", self._opencv_index)

    def _apply_mvs_exposure(self, cam) -> None:
        """把曝光配置写入海康 MVS 相机。"""

        exposure_auto = self._exposure_auto
        if self._exposure_time_us is not None and exposure_auto is None:
            exposure_auto = False

        if exposure_auto is not None:
            # 海康 MVS 常用枚举：0=Off，1=Once，2=Continuous。
            ret = cam.MV_CC_SetEnumValue("ExposureAuto", 2 if exposure_auto else 0)
            self._check_mvs(ret, "set ExposureAuto")

        if self._exposure_time_us is not None:
            if self._exposure_time_us <= 0:
                raise ValueError("exposure_time_us 必须大于 0")
            ret = cam.MV_CC_SetFloatValue("ExposureTime", float(self._exposure_time_us))
            self._check_mvs(ret, "set ExposureTime")
            logger.info("MVS 手动曝光时间已设置: %.1f us", self._exposure_time_us)

    def _apply_opencv_exposure(self) -> None:
        """尽量把曝光配置写入 OpenCV 相机。"""

        if self._cap is None:
            return

        if self._exposure_auto is not None:
            # 不同 OpenCV 后端含义略有差异；DirectShow 常用 0.75=自动，0.25=手动。
            self._cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75 if self._exposure_auto else 0.25)

        if self._exposure_time_us is not None:
            logger.warning(
                "OpenCV 后端不支持按微秒精确设置 exposure_time_us；请优先使用 MVS 后端。"
            )

    def capture(self) -> np.ndarray:
        """实时采一帧，返回 BGR (H,W,3) uint8。"""
        # MVS 打开成功时走 MVS 取帧。
        if self._use_mvs and self._mvs_handle is not None:
            return self._capture_mvs()
        # 否则走 OpenCV 取帧。
        return self._capture_opencv()

    def _capture_mvs(self) -> np.ndarray:
        """通过 MVS 取一帧并转换为 BGR numpy。"""

        # 取出 SDK 模块。
        mvs = self._mvs
        # 取出相机句柄。
        cam = self._mvs_handle
        # 如果句柄为空，说明还没有打开成功。
        if mvs is None or cam is None:
            raise RuntimeError("MVS 相机尚未打开")

        # 创建 MVS 帧输出结构体。
        st_frame = mvs.MV_FRAME_OUT()
        # 清零结构体内存，符合 SDK 示例写法。
        memset(byref(st_frame), 0, sizeof(st_frame))
        # 从 SDK 缓冲区取一帧，超时时间 1000ms。
        ret = cam.MV_CC_GetImageBuffer(st_frame, 1000)
        # ret 非 0 或指针为空都表示取图失败。
        if ret != 0 or st_frame.pBufAddr is None:
            raise RuntimeError(f"MVS 取图失败，ret=0x{ret:x}")

        try:
            # 读取图像宽度。
            width = int(st_frame.stFrameInfo.nWidth)
            # 读取图像高度。
            height = int(st_frame.stFrameInfo.nHeight)
            # 目标 RGB8 图像需要宽*高*3 字节。
            rgb_size = width * height * 3
            # 分配目标 RGB 缓冲区。
            rgb_buffer = (c_ubyte * rgb_size)()

            # 创建像素格式转换参数结构体。
            convert_param = mvs.MV_CC_PIXEL_CONVERT_PARAM_EX()
            # 清零结构体，避免未初始化字段影响 SDK。
            memset(byref(convert_param), 0, sizeof(convert_param))
            # 设置源图宽度。
            convert_param.nWidth = width
            # 设置源图高度。
            convert_param.nHeight = height
            # 设置源数据指针，来自 MVS 帧缓冲区。
            convert_param.pSrcData = st_frame.pBufAddr
            # 设置源数据长度。
            convert_param.nSrcDataLen = st_frame.stFrameInfo.nFrameLen
            # 设置源像素格式，例如 Bayer/Mono/RGB 等。
            convert_param.enSrcPixelType = st_frame.stFrameInfo.enPixelType
            # 目标统一转换成 RGB8，后面再转 OpenCV 的 BGR。
            convert_param.enDstPixelType = mvs.PixelType_Gvsp_RGB8_Packed
            # 设置目标缓冲区指针。
            convert_param.pDstBuffer = cast(rgb_buffer, POINTER(c_ubyte))
            # 设置目标缓冲区大小。
            convert_param.nDstBufferSize = rgb_size

            # 调用 SDK 做像素格式转换。
            ret = cam.MV_CC_ConvertPixelTypeEx(convert_param)
            self._check_mvs(ret, "convert pixel type")

            # 把 ctypes 缓冲区转成 numpy 一维数组。
            rgb = np.frombuffer(rgb_buffer, dtype=np.uint8, count=int(convert_param.nDstLen))
            # 改成 HxWx3 图像，并 copy 脱离 SDK 临时缓冲。
            rgb = rgb.reshape((height, width, 3)).copy()
            # OpenCV 使用 BGR 顺序，所以从 RGB 转成 BGR。
            frame = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            # 记录取帧次数。
            self._capture_count += 1
            # 首帧成功时打印一次，方便确认真的走了 MVS。
            if self._capture_count == 1:
                logger.info("MVS 首帧取图成功: %sx%s", width, height)
            return frame
        finally:
            # 每次 GetImageBuffer 后都必须 FreeImageBuffer，否则 SDK 缓冲会耗尽。
            cam.MV_CC_FreeImageBuffer(st_frame)

    def _capture_opencv(self) -> np.ndarray:
        # 从 OpenCV 摄像头读取一帧。
        ret, frame = self._cap.read()
        # 读取失败时返回黑图，避免上层算法直接崩溃。
        if not ret or frame is None:
            return np.zeros((480, 640, 3), dtype=np.uint8)
        # 记录取帧次数。
        self._capture_count += 1
        # 首帧成功时打印一次。
        if self._capture_count == 1:
            logger.info("OpenCV 首帧取图成功: %sx%s", frame.shape[1], frame.shape[0])
        return frame

    def close(self) -> None:
        # 如果 MVS 句柄存在，先停止采流，再关闭设备。
        if self._mvs_handle is not None:
            if self._mvs_grabbing:
                self._mvs_handle.MV_CC_StopGrabbing()
                self._mvs_grabbing = False
            self._mvs_handle.MV_CC_CloseDevice()
            self._mvs_handle.MV_CC_DestroyHandle()
            self._mvs_handle = None
        # 如果初始化过 MVS SDK，关闭时反初始化。
        if self._mvs_initialized and self._mvs is not None:
            self._mvs.MvCamera.MV_CC_Finalize()
            self._mvs_initialized = False
        # 如果 OpenCV 摄像头存在，释放它。
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _load_mvs_module(self):
        # 找到 MvImport 路径。
        sdk_path = self._resolve_mvs_sdk_path()
        # 把路径加入 sys.path，方便 import MvCameraControl_class。
        if sdk_path and str(sdk_path) not in sys.path:
            sys.path.append(str(sdk_path))
        try:
            # 导入海康 MVS Python 封装。
            return importlib.import_module("MvCameraControl_class")
        except ImportError as exc:
            raise RuntimeError(
                "无法导入海康 MVS Python SDK。请确认 camera.mvs_sdk_path 指向 MvImport 目录。"
            ) from exc

    def _resolve_mvs_sdk_path(self) -> Optional[Path]:
        # 候选 SDK 路径列表，按优先级尝试。
        candidates = []
        # 配置文件里写的路径优先。
        if self._mvs_sdk_path:
            candidates.append(Path(str(self._mvs_sdk_path)))
        # MVS 安装后通常会设置 MVCAM_COMMON_RUNENV 环境变量。
        env_root = os.getenv("MVCAM_COMMON_RUNENV")
        if env_root:
            candidates.append(Path(env_root) / "Samples" / "Python" / "MvImport")
        # 常见安装路径兜底。
        candidates.extend(
            [
                Path("D:/app/mvs/MVS/Development/Samples/Python/MvImport"),
                Path("C:/Program Files (x86)/MVS/Development/Samples/Python/MvImport"),
                Path("C:/Program Files/MVS/Development/Samples/Python/MvImport"),
            ]
        )
        # 找到包含 MvCameraControl_class.py 的目录就返回。
        for path in candidates:
            if (path / "MvCameraControl_class.py").exists():
                return path
        # 没找到时返回 None，后续 import 会失败并给出提示。
        return None

    def _make_mvs_device_info(self, mvs):
        # 如果配置了 IP，就按指定 IP 构造设备信息。
        if self._device:
            return self._make_mvs_device_info_from_ip(mvs, str(self._device))

        # 没配置 IP 时，调用 SDK 枚举设备。
        device_list = mvs.MV_CC_DEVICE_INFO_LIST()
        # 枚举 GigE 和 USB 设备。
        tlayer_type = mvs.MV_GIGE_DEVICE | mvs.MV_USB_DEVICE
        # 新版 SDK 可能还有 GenTL GigE 类型。
        if hasattr(mvs, "MV_GENTL_GIGE_DEVICE"):
            tlayer_type |= mvs.MV_GENTL_GIGE_DEVICE
        # 调用 SDK 枚举设备。
        ret = mvs.MvCamera.MV_CC_EnumDevices(tlayer_type, device_list)
        self._check_mvs(ret, "enum devices")
        # 没枚举到设备时直接报错。
        if device_list.nDeviceNum == 0:
            raise RuntimeError("MVS 未枚举到相机")
        # 默认使用第一个枚举到的设备。
        return cast(device_list.pDeviceInfo[0], POINTER(mvs.MV_CC_DEVICE_INFO)).contents

    def _make_mvs_device_info_from_ip(self, mvs, camera_ip: str):
        # 使用配置的网卡 IP；没配时尝试自动推断。
        net_export_ip = self._net_export_ip or self._detect_local_ip(camera_ip)
        # 创建设备信息结构体。
        st_device_info = mvs.MV_CC_DEVICE_INFO()
        # 创建 GigE 设备信息结构体。
        st_gige_info = mvs.MV_GIGE_DEVICE_INFO()
        # 填入相机 IP。
        st_gige_info.nCurrentIp = self._ip_to_int(camera_ip)
        # 填入电脑网卡 IP。
        st_gige_info.nNetExport = self._ip_to_int(net_export_ip)
        # 指明这是 GigE 相机。
        st_device_info.nTLayerType = mvs.MV_GIGE_DEVICE
        # 把 GigE 信息放入设备信息联合体。
        st_device_info.SpecialInfo.stGigEInfo = st_gige_info
        return st_device_info

    def _detect_local_ip(self, camera_ip: str) -> str:
        # 创建 UDP socket，不真正发数据，只用于让系统选择路由出口。
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 连接到相机 GVCP 端口，系统会选出对应本机 IP。
            sock.connect((camera_ip, 3956))
            return sock.getsockname()[0]
        finally:
            # 关闭临时 socket。
            sock.close()

    @staticmethod
    def _ip_to_int(ip: str) -> int:
        # 把 "192.168.1.253" 拆成四段整数。
        parts = [int(part) for part in ip.split(".")]
        # 校验 IP 必须正好四段且每段 0~255。
        if len(parts) != 4 or any(part < 0 or part > 255 for part in parts):
            raise ValueError(f"IP 地址格式错误: {ip}")
        # MVS SDK 需要大端整数格式的 IP。
        return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]

    @staticmethod
    def _check_mvs(ret: int, action: str) -> None:
        # MVS SDK 返回 0 表示成功，非 0 表示错误码。
        if ret != 0:
            raise RuntimeError(f"MVS {action} 失败，ret=0x{ret:x}")
