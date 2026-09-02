"""定义 workflow API 请求体模型及基础字段校验规则。"""
from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, ConfigDict, Field


class StrictApiRequest(BaseModel):
    """Reject misspelled or undocumented top-level API fields."""

    model_config = ConfigDict(extra="forbid")


class ExecuteTaskRequest(StrictApiRequest):
    task: Dict[str, Any]
    camera_path: str | None = Field(default=None, description="可选，覆盖默认 camera.yaml")
    objectives_path: str | None = Field(default=None, description="可选，覆盖默认 objectives.yaml")
    plates_path: str | None = Field(default=None, description="可选，覆盖默认 plates.yaml")
    dump_json: str | None = Field(default=None, description="可选，覆盖结果落盘路径")
    persist_result: bool = Field(default=True, description="是否仍然把结果写到本地文件")


class CameraRecordStartRequest(StrictApiRequest):
    save_path: str = Field(default="data/camera_records/recording.avi", min_length=1)
    camera_path: str | None = None
    device_index: int | None = Field(default=None, ge=0, le=63)
    serial_number: str | None = None
    ip: str | None = None
    mvs_python_dir: str | None = None
    pixel_format: str | None = None
    exposure_us: float | None = Field(default=None, gt=0, le=10_000_000)
    gain: float | None = Field(default=None, ge=0, le=60)
    fps: float | None = Field(default=10.0, gt=0, le=240)
    bitrate_kbps: int = Field(default=1000, ge=1, le=500_000)
    timeout_ms: int | None = Field(
        default=None,
        gt=0,
        le=15_000,
        description="录像取帧超时，毫秒；须覆盖曝光时间加传输余量，上限 15000",
    )


class CameraRecordStopRequest(StrictApiRequest):
    """Empty body contract used to reject accidental stop parameters."""


class StageReciprocationStartRequest(StrictApiRequest):
    port: str = Field(default="COM3", min_length=1, max_length=64, description="XY 位移台 Modbus 串口号")
    baudrate: int = Field(default=115200, ge=1200, le=921600, description="Modbus 串口波特率")
    x_slave: int = Field(default=1, ge=1, le=247, description="X 轴 Modbus 从站地址")
    y_slave: int = Field(default=2, ge=1, le=247, description="Y 轴 Modbus 从站地址")
    profile_vel: int = Field(default=800000, ge=1, le=10_000_000, description="PP 位置模式轮廓速度")
    profile_acc: int = Field(default=800000, ge=1, le=10_000_000, description="PP 位置模式轮廓加速度")
    profile_dec: int = Field(default=800000, ge=1, le=10_000_000, description="PP 位置模式轮廓减速度")
    arrival_tolerance: int = Field(default=80, ge=0, le=1_000_000, description="到位容差，单位 pulse")
    poll_s: float = Field(default=0.05, gt=0, le=10, description="运动中当前位置轮询间隔，单位秒")
    settle_s: float = Field(default=0.2, ge=0, le=60, description="到达点位后的稳定等待时间，单位秒")
    move_timeout_s: float = Field(default=120.0, gt=0, le=3600, description="单次点到点移动超时时间，单位秒")
    max_cycles: int | None = Field(default=None, ge=1, le=1_000_000, description="最大往复周期数；不传表示持续运行直到 stop")


class StageReciprocationStopRequest(StrictApiRequest):
    join_timeout_s: float = Field(default=5.0, ge=0, le=120, description="等待后台线程停止的最长时间，单位秒")
