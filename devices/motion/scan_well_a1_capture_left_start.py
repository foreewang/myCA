from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List

CURRENT_DIR = Path(__file__).resolve().parent
PARENT_DIR = CURRENT_DIR.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from camera_controller import HikCameraController  # type: ignore
from modbus import ModbusRTUClient  # type: ignore
from MotorManager import MotorManager, point_12, point_12_d, rpm_mm  # type: ignore


WELL_DIAMETER_MM = point_12_d / 10.0
PULSES_PER_MM = rpm_mm * 10.0
A1_LEFT_START_X = int(point_12[0])
A1_LEFT_START_Y = int(point_12[1])


@dataclass
class ScanPoint:
    index: int
    row_index: int
    col_index: int
    view_down_mm: float
    view_right_mm: float
    stage_x_target: int
    stage_y_target: int
    save_path: str



def round_int(x: float) -> int:
    return int(round(x))



def generate_line_positions(start_mm: float, end_mm: float, step_mm: float) -> List[float]:
    if step_mm <= 0:
        raise ValueError("step_mm must be > 0")
    vals: List[float] = []
    v = start_mm
    while v <= end_mm + 1e-9:
        vals.append(round(v, 6))
        v += step_mm
    if not vals:
        vals = [round(start_mm, 6)]
    if abs(vals[-1] - end_mm) > 1e-6:
        vals.append(round(end_mm, 6))
    return vals



def generate_vertical_offsets_center_out(radius_mm: float, step_mm: float) -> List[float]:
    offsets = [0.0]
    k = 1
    while True:
        d = k * step_mm
        if d > radius_mm + 1e-9:
            break
        offsets.append(round(-d, 6))
        offsets.append(round(+d, 6))
        k += 1
    return offsets



def build_a1_scan_points(
    save_dir: Path,
    fov_mm: float,
    overlap: float,
    start_x: int = A1_LEFT_START_X,
    start_y: int = A1_LEFT_START_Y,
    x_stage_sign_for_view_down: int = -1,
    y_stage_sign_for_view_right: int = -1,
) -> List[ScanPoint]:
    if not (0.0 <= overlap < 1.0):
        raise ValueError("overlap must be in [0, 1)")
    if fov_mm <= 0:
        raise ValueError("fov_mm must be > 0")

    radius_mm = WELL_DIAMETER_MM / 2.0
    step_mm = fov_mm * (1.0 - overlap)
    if step_mm <= 0:
        raise ValueError("fov step <= 0; check fov_mm and overlap")

    # 以 A1 左侧起始点为原点：
    # - 视野向右为 +view_right_mm
    # - 视野向下为 +view_down_mm
    # 圆心在 (view_right_mm=radius_mm, view_down_mm=0)
    row_offsets = generate_vertical_offsets_center_out(radius_mm, step_mm)

    points: List[ScanPoint] = []
    idx = 1
    for row_i, view_down_mm in enumerate(row_offsets):
        half_width = math.sqrt(max(0.0, radius_mm * radius_mm - view_down_mm * view_down_mm))
        left_mm = radius_mm - half_width
        right_mm = radius_mm + half_width
        row_positions = generate_line_positions(left_mm, right_mm, step_mm)
        if row_i % 2 == 1:
            row_positions = list(reversed(row_positions))

        for col_i, view_right_mm in enumerate(row_positions):
            stage_x = round_int(start_x + x_stage_sign_for_view_down * view_down_mm * PULSES_PER_MM)
            stage_y = round_int(start_y + y_stage_sign_for_view_right * view_right_mm * PULSES_PER_MM)
            filename = (
                f"A1_{idx:03d}_row{row_i:02d}_col{col_i:02d}"
                f"_vdown_{view_down_mm:+.3f}_vright_{view_right_mm:+.3f}"
                f"_x_{stage_x}_y_{stage_y}.bmp"
            )
            points.append(
                ScanPoint(
                    index=idx,
                    row_index=row_i,
                    col_index=col_i,
                    view_down_mm=view_down_mm,
                    view_right_mm=view_right_mm,
                    stage_x_target=stage_x,
                    stage_y_target=stage_y,
                    save_path=str(save_dir / filename),
                )
            )
            idx += 1
    return points



def read_axis_snapshot(client: ModbusRTUClient, axis_name: str, slave: int) -> Dict[str, Any]:
    pos = client._read_32bit(slave, client.REG_CURRENT_POS)
    sw = client._read_statusword(slave)
    return {
        "axis": axis_name,
        "slave": slave,
        "current_pos": pos,
        "statusword": sw,
    }



def move_absolute_xy(
    x_motor: MotorManager,
    y_motor: MotorManager,
    x_target: int,
    y_target: int,
    profile_vel: int,
    profile_acc: int,
    profile_dec: int,
    timeout: float,
) -> Dict[str, Any]:
    x_diff = x_motor.pp_absolute_move(
        target_pos=x_target,
        profile_vel=profile_vel,
        profile_acc=profile_acc,
        profile_dec=profile_dec,
        timeout=timeout,
    )
    y_diff = y_motor.pp_absolute_move(
        target_pos=y_target,
        profile_vel=profile_vel,
        profile_acc=profile_acc,
        profile_dec=profile_dec,
        timeout=timeout,
    )
    return {"x_diff": x_diff, "y_diff": y_diff}



def main() -> None:
    parser = argparse.ArgumentParser(description="Scan 12-well A1 from left-start reference point and capture images")
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--x-slave", type=int, default=1)
    parser.add_argument("--y-slave", type=int, default=2)
    parser.add_argument("--well", default="A1", help="当前版本仅支持 A1")
    parser.add_argument("--save-dir", required=True)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--fov-mm", type=float, default=3.0, help="单张图视野边长，单位 mm")
    parser.add_argument("--overlap", type=float, default=0.15, help="相邻视野重叠率")
    parser.add_argument("--profile-vel", type=int, default=200000)
    parser.add_argument("--profile-acc", type=int, default=50000)
    parser.add_argument("--profile-dec", type=int, default=50000)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--settle-s", type=float, default=0.8)
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--serial-number", default=None)
    parser.add_argument("--mvs-python-dir", default=None)
    parser.add_argument("--exposure-us", type=float, default=None)
    parser.add_argument("--gain", type=float, default=None)
    parser.add_argument("--start-x", type=int, default=A1_LEFT_START_X, help="A1 左侧起始点 X 脉冲")
    parser.add_argument("--start-y", type=int, default=A1_LEFT_START_Y, help="A1 左侧起始点 Y 脉冲")
    parser.add_argument("--x-stage-sign-for-view-down", type=int, choices=[-1, 1], default=-1,
                        help="视野向下时，位移台 X 脉冲变化符号；已按你当前描述默认 -1")
    parser.add_argument("--y-stage-sign-for-view-right", type=int, choices=[-1, 1], default=-1,
                        help="视野向右时，位移台 Y 脉冲变化符号；已按你当前描述默认 -1")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.well.upper() != "A1":
        raise SystemExit("当前版本仅支持 A1。")

    save_dir = Path(args.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    scan_points = build_a1_scan_points(
        save_dir=save_dir,
        fov_mm=args.fov_mm,
        overlap=args.overlap,
        start_x=args.start_x,
        start_y=args.start_y,
        x_stage_sign_for_view_down=args.x_stage_sign_for_view_down,
        y_stage_sign_for_view_right=args.y_stage_sign_for_view_right,
    )

    manifest: Dict[str, Any] = {
        "plate_type": "12-well",
        "well": "A1",
        "reference": {
            "meaning": "A1 左侧起始点（视野位于孔正左侧中线）",
            "start_x": args.start_x,
            "start_y": args.start_y,
            "well_diameter_mm": WELL_DIAMETER_MM,
            "pulses_per_mm": PULSES_PER_MM,
            "x_stage_sign_for_view_down": args.x_stage_sign_for_view_down,
            "y_stage_sign_for_view_right": args.y_stage_sign_for_view_right,
        },
        "scan_config": {
            "fov_mm": args.fov_mm,
            "overlap": args.overlap,
            "profile_vel": args.profile_vel,
            "profile_acc": args.profile_acc,
            "profile_dec": args.profile_dec,
            "timeout": args.timeout,
            "settle_s": args.settle_s,
            "point_count": len(scan_points),
        },
        "points": [asdict(p) for p in scan_points],
        "captures": [],
    }

    if args.dry_run:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        if args.output_json:
            Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
            with open(args.output_json, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
        return

    cam = HikCameraController(
        mvs_python_dir=args.mvs_python_dir,
        device_index=args.device_index,
        serial_number=args.serial_number,
        default_exposure_us=args.exposure_us,
        default_gain=args.gain,
    )

    cam.open()
    try:
        with ModbusRTUClient(port=args.port, baudrate=args.baudrate) as client:
            x_motor = MotorManager(client, slave=args.x_slave)
            y_motor = MotorManager(client, slave=args.y_slave)

            for p in scan_points:
                before = {
                    "x": read_axis_snapshot(client, "x", args.x_slave),
                    "y": read_axis_snapshot(client, "y", args.y_slave),
                }

                move_result = move_absolute_xy(
                    x_motor=x_motor,
                    y_motor=y_motor,
                    x_target=p.stage_x_target,
                    y_target=p.stage_y_target,
                    profile_vel=args.profile_vel,
                    profile_acc=args.profile_acc,
                    profile_dec=args.profile_dec,
                    timeout=args.timeout,
                )

                time.sleep(args.settle_s)
                frame = cam.capture_bmp(p.save_path)

                after = {
                    "x": read_axis_snapshot(client, "x", args.x_slave),
                    "y": read_axis_snapshot(client, "y", args.y_slave),
                }

                manifest["captures"].append(
                    {
                        "index": p.index,
                        "row_index": p.row_index,
                        "col_index": p.col_index,
                        "view_down_mm": p.view_down_mm,
                        "view_right_mm": p.view_right_mm,
                        "stage_x_target": p.stage_x_target,
                        "stage_y_target": p.stage_y_target,
                        "before": before,
                        "move_result": move_result,
                        "after": after,
                        "frame": {
                            "width": frame.width,
                            "height": frame.height,
                            "frame_num": frame.frame_num,
                            "pixel_type": frame.pixel_type,
                            "frame_len": frame.frame_len,
                            "saved_path": frame.saved_path,
                            "timestamp": frame.timestamp,
                        },
                    }
                )
                print(f"[{p.index}/{len(scan_points)}] captured -> {p.save_path}")
    finally:
        cam.close()

    if args.output_json:
        out_json = Path(args.output_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(json.dumps({
        "well": "A1",
        "point_count": len(scan_points),
        "save_dir": str(save_dir),
        "output_json": args.output_json,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
