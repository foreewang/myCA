from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Dict, Any

from modbus import ModbusRTUClient
from MotorManager import MotorManager, point_12, point_12_d, point_12_gap, rpm_mm


def snapshot_axis(motor: MotorManager, axis_name: str) -> Dict[str, Any]:
    pos = motor.client._read_32bit(motor.slave, motor.client.REG_CURRENT_POS)
    sw = motor.client._read_statusword(motor.slave)
    return {
        "axis": axis_name,
        "slave": motor.slave,
        "current_pos": pos,
        "statusword": sw,
    }


def snapshot_xy(x_motor: MotorManager, y_motor: MotorManager) -> Dict[str, Any]:
    return {
        "x": snapshot_axis(x_motor, "x"),
        "y": snapshot_axis(y_motor, "y"),
    }


def move_abs_xy(
    x_motor: MotorManager,
    y_motor: MotorManager,
    x_target: int,
    y_target: int,
    profile_vel: int,
    profile_acc: int,
    profile_dec: int,
    timeout: float,
    y_first: bool = False,
) -> Dict[str, Any]:
    before = snapshot_xy(x_motor, y_motor)
    if y_first:
        y_diff = y_motor.pp_absolute_move(y_target, profile_vel, profile_acc, profile_dec, timeout)
        x_diff = x_motor.pp_absolute_move(x_target, profile_vel, profile_acc, profile_dec, timeout)
    else:
        x_diff = x_motor.pp_absolute_move(x_target, profile_vel, profile_acc, profile_dec, timeout)
        y_diff = y_motor.pp_absolute_move(y_target, profile_vel, profile_acc, profile_dec, timeout)
    after = snapshot_xy(x_motor, y_motor)
    return {
        "target": {"x": x_target, "y": y_target},
        "before": before,
        "move_result": {"x_diff": x_diff, "y_diff": y_diff},
        "after": after,
        "err_to_target": {
            "x": None if after["x"]["current_pos"] is None else after["x"]["current_pos"] - x_target,
            "y": None if after["y"]["current_pos"] is None else after["y"]["current_pos"] - y_target,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="A1 <-> C4 往复精度测试")
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--x-slave", type=int, default=1)
    parser.add_argument("--y-slave", type=int, default=2)
    parser.add_argument("--profile-vel", type=int, default=200000)
    parser.add_argument("--profile-acc", type=int, default=50000)
    parser.add_argument("--profile-dec", type=int, default=50000)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--settle-s", type=float, default=0.8)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--y-first", action="store_true")
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args()

    # 你更正后的规则：x 与 y 都按 (孔径 + 孔间隙) 做孔中心节距
    pitch_pulse = int((point_12_d + point_12_gap) * rpm_mm)

    a1_x, a1_y = point_12
    # A1 -> C4: 行偏移 2，列偏移 3
    c4_x = a1_x - 2 * pitch_pulse
    c4_y = a1_y - 3 * pitch_pulse

    result: Dict[str, Any] = {
        "plate": "12-well",
        "rule": "x_step = y_step = (point_12_d + point_12_gap) * rpm_mm",
        "point_12": {"x": a1_x, "y": a1_y},
        "point_12_d": point_12_d,
        "point_12_gap": point_12_gap,
        "rpm_mm": rpm_mm,
        "pitch_pulse": pitch_pulse,
        "A1": {"x": a1_x, "y": a1_y},
        "C4": {"x": c4_x, "y": c4_y},
        "cycles": args.cycles,
        "profile": {
            "vel": args.profile_vel,
            "acc": args.profile_acc,
            "dec": args.profile_dec,
            "timeout": args.timeout,
            "settle_s": args.settle_s,
            "y_first": args.y_first,
        },
        "records": [],
    }

    with ModbusRTUClient(port=args.port, baudrate=args.baudrate) as client:
        x_motor = MotorManager(client, slave=args.x_slave)
        y_motor = MotorManager(client, slave=args.y_slave)

        for i in range(1, args.cycles + 1):
            rec: Dict[str, Any] = {"cycle": i}

            rec["to_A1_before"] = snapshot_xy(x_motor, y_motor)
            rec["go_to_A1"] = move_abs_xy(
                x_motor, y_motor, a1_x, a1_y,
                args.profile_vel, args.profile_acc, args.profile_dec, args.timeout,
                y_first=args.y_first,
            )
            time.sleep(args.settle_s)

            rec["go_A1_to_C4"] = move_abs_xy(
                x_motor, y_motor, c4_x, c4_y,
                args.profile_vel, args.profile_acc, args.profile_dec, args.timeout,
                y_first=args.y_first,
            )
            time.sleep(args.settle_s)

            rec["back_C4_to_A1"] = move_abs_xy(
                x_motor, y_motor, a1_x, a1_y,
                args.profile_vel, args.profile_acc, args.profile_dec, args.timeout,
                y_first=args.y_first,
            )
            time.sleep(args.settle_s)

            result["records"].append(rec)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"saved to: {out_path}")


if __name__ == "__main__":
    main()

