"""定义 workflow API 请求体模型及基础字段校验规则。"""
from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field, model_validator


class ExecuteTaskRequest(BaseModel):
    task: Dict[str, Any]
    camera_path: str | None = Field(default=None, description="可选，覆盖默认 camera.yaml")
    objectives_path: str | None = Field(default=None, description="可选，覆盖默认 objectives.yaml")
    plates_path: str | None = Field(default=None, description="可选，覆盖默认 plates.yaml")
    dump_json: str | None = Field(default=None, description="可选，覆盖结果落盘路径")
    persist_result: bool = Field(default=True, description="是否仍然把结果写到本地文件")


class CameraRecordStartRequest(BaseModel):
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
    timeout_ms: int | None = Field(default=None, gt=0, le=600_000)


class StageReciprocationStartRequest(BaseModel):
    port: str = Field(default="COM3", min_length=1, max_length=64, description="XY 位移台 Modbus 串口号")
    baudrate: int = Field(default=115200, ge=1200, le=921600, description="Modbus 串口波特率")
    x_slave: int = Field(default=1, ge=1, le=247, description="X 轴 Modbus 从站地址")
    y_slave: int = Field(default=2, ge=1, le=247, description="Y 轴 Modbus 从站地址")
    point_a_x: int = Field(default=0, ge=-100_000_000, le=100_000_000, description="往复点 A 的 X 坐标，单位 pulse")
    point_a_y: int = Field(default=7500000, ge=-100_000_000, le=100_000_000, description="往复点 A 的 Y 坐标，单位 pulse")
    point_b_x: int = Field(default=8865800, ge=-100_000_000, le=100_000_000, description="往复点 B 的 X 坐标，单位 pulse")
    point_b_y: int = Field(default=-550000, ge=-100_000_000, le=100_000_000, description="往复点 B 的 Y 坐标，单位 pulse")
    profile_vel: int = Field(default=800000, ge=1, le=10_000_000, description="PP 位置模式轮廓速度")
    profile_acc: int = Field(default=800000, ge=1, le=10_000_000, description="PP 位置模式轮廓加速度")
    profile_dec: int = Field(default=800000, ge=1, le=10_000_000, description="PP 位置模式轮廓减速度")
    arrival_tolerance: int = Field(default=80, ge=0, le=1_000_000, description="到位容差，单位 pulse")
    poll_s: float = Field(default=0.05, gt=0, le=10, description="运动中当前位置轮询间隔，单位秒")
    settle_s: float = Field(default=0.2, ge=0, le=60, description="到达点位后的稳定等待时间，单位秒")
    move_timeout_s: float = Field(default=120.0, gt=0, le=3600, description="单次点到点移动超时时间，单位秒")
    max_cycles: int | None = Field(default=None, ge=1, le=1_000_000, description="最大往复周期数；不传表示持续运行直到 stop")
    limit_check_enabled: bool = Field(default=True, description="是否启用位置安全限位检查")
    x_min: int = Field(default=-800000, ge=-100_000_000, le=100_000_000, description="X 轴机械最小限位，单位 pulse")
    x_max: int = Field(default=10400000, ge=-100_000_000, le=100_000_000, description="X 轴机械最大限位，单位 pulse")
    y_min: int = Field(default=-8900000, ge=-100_000_000, le=100_000_000, description="Y 轴机械最小限位，单位 pulse")
    y_max: int = Field(default=7700000, ge=-100_000_000, le=100_000_000, description="Y 轴机械最大限位，单位 pulse")
    safety_margin: int = Field(default=147500, ge=0, le=10_000_000, description="限位安全边界，单位 pulse")

    @model_validator(mode="after")
    def validate_motion_safety(self):
        if self.x_min >= self.x_max:
            raise ValueError("x_min must be smaller than x_max")
        if self.y_min >= self.y_max:
            raise ValueError("y_min must be smaller than y_max")
        if self.safety_margin * 2 >= (self.x_max - self.x_min):
            raise ValueError("safety_margin leaves no usable X travel range")
        if self.safety_margin * 2 >= (self.y_max - self.y_min):
            raise ValueError("safety_margin leaves no usable Y travel range")

        if self.limit_check_enabled:
            x_lo = self.x_min + self.safety_margin
            x_hi = self.x_max - self.safety_margin
            y_lo = self.y_min + self.safety_margin
            y_hi = self.y_max - self.safety_margin
            for name, x, y in (
                ("point_a", self.point_a_x, self.point_a_y),
                ("point_b", self.point_b_x, self.point_b_y),
            ):
                if x < x_lo or x > x_hi:
                    raise ValueError(f"{name}.x={x} is outside safe X range [{x_lo}, {x_hi}]")
                if y < y_lo or y > y_hi:
                    raise ValueError(f"{name}.y={y} is outside safe Y range [{y_lo}, {y_hi}]")
        return self


class StageReciprocationStopRequest(BaseModel):
    join_timeout_s: float = Field(default=5.0, ge=0, le=120, description="等待后台线程停止的最长时间，单位秒")
