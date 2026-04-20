from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from modbus import ModbusRTUClient
from MotorManager import MotorManager


def read_current_pos(client: ModbusRTUClient, slave: int) -> Optional[int]:
    return client._read_32bit(slave, client.REG_CURRENT_POS)


def read_statusword(client: ModbusRTUClient, slave: int) -> Optional[int]:
    return client._read_statusword(slave)


def dump_axis_state(client: ModbusRTUClient, axis_name: str, slave: int) -> dict:
    return {
        "axis": axis_name,
        "slave": slave,
        "current_pos": read_current_pos(client, slave),
        "statusword": read_statusword(client, slave),
    }


def move_absolute(
    motor: MotorManager,
    target_pos: Optional[int],
    profile_vel: int,
    profile_acc: int,
    profile_dec: int,
    timeout: float,
) -> Optional[int]:
    if target_pos is None:
        return None
    return motor.pp_absolute_move(
        target_pos=target_pos,
        profile_vel=profile_vel,
        profile_acc=profile_acc,
        profile_dec=profile_dec,
        timeout=timeout,
    )


def move_relative(
    motor: MotorManager,
    offset: Optional[int],
    profile_vel: int,
    profile_acc: int,
    profile_dec: int,
    timeout: float,
) -> Optional[int]:
    if offset is None:
        return None
    return motor.pp_relative_move(
        offset=offset,
        profile_vel=profile_vel,
        profile_acc=profile_acc,
        profile_dec=profile_dec,
        timeout=timeout,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="XY 位移台测试脚本（基于 modbus.py + MotorManager.py）"
    )
    parser.add_argument("--port", default="COM3", help="串口号，例如 COM3")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--x-slave", type=int, default=1, help="X 轴从站号")
    parser.add_argument("--y-slave", type=int, default=2, help="Y 轴从站号")
    parser.add_argument(
        "--mode",
        choices=["status", "abs", "rel"],
        default="status",
        help="status=只读状态；abs=绝对运动；rel=相对运动",
    )

    parser.add_argument("--x", type=int, default=None, help="X 轴绝对目标位置（脉冲）")
    parser.add_argument("--y", type=int, default=None, help="Y 轴绝对目标位置（脉冲）")
    parser.add_argument("--dx", type=int, default=None, help="X 轴相对位移（脉冲）")
    parser.add_argument("--dy", type=int, default=None, help="Y 轴相对位移（脉冲）")

    parser.add_argument("--profile-vel", type=int, default=200000, help="轮廓速度（脉冲/秒）")
    parser.add_argument("--profile-acc", type=int, default=50000, help="轮廓加速度（脉冲/秒²）")
    parser.add_argument("--profile-dec", type=int, default=50000, help="轮廓减速度（脉冲/秒²）")
    parser.add_argument("--timeout", type=float, default=120.0, help="单次运动超时时间（秒）")
    parser.add_argument(
        "--y-first",
        action="store_true",
        help="默认先走 X 再走 Y；加此参数则先走 Y 再走 X",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        help="可选：把测试结果写入 JSON 文件",
    )
    return parser



def main() -> None:
    args = build_parser().parse_args()

    with ModbusRTUClient(port=args.port, baudrate=args.baudrate) as client:
        if not client.is_connected():
            raise RuntimeError(f"串口连接失败: {args.port}")

        x_motor = MotorManager(client, slave=args.x_slave)
        y_motor = MotorManager(client, slave=args.y_slave)

        result = {
            "port": args.port,
            "baudrate": args.baudrate,
            "mode": args.mode,
            "before": {
                "x": dump_axis_state(client, "x", args.x_slave),
                "y": dump_axis_state(client, "y", args.y_slave),
            },
            "command": {
                "x": args.x,
                "y": args.y,
                "dx": args.dx,
                "dy": args.dy,
                "profile_vel": args.profile_vel,
                "profile_acc": args.profile_acc,
                "profile_dec": args.profile_dec,
                "timeout": args.timeout,
                "y_first": args.y_first,
            },
            "move_result": {},
            "after": None,
        }

        if args.mode == "status":
            pass
        elif args.mode == "abs":
            order = ["y", "x"] if args.y_first else ["x", "y"]
            for axis in order:
                if axis == "x":
                    diff = move_absolute(
                        x_motor,
                        args.x,
                        args.profile_vel,
                        args.profile_acc,
                        args.profile_dec,
                        args.timeout,
                    )
                    result["move_result"]["x_diff"] = diff
                else:
                    diff = move_absolute(
                        y_motor,
                        args.y,
                        args.profile_vel,
                        args.profile_acc,
                        args.profile_dec,
                        args.timeout,
                    )
                    result["move_result"]["y_diff"] = diff
        elif args.mode == "rel":
            order = ["y", "x"] if args.y_first else ["x", "y"]
            for axis in order:
                if axis == "x":
                    diff = move_relative(
                        x_motor,
                        args.dx,
                        args.profile_vel,
                        args.profile_acc,
                        args.profile_dec,
                        args.timeout,
                    )
                    result["move_result"]["x_diff"] = diff
                else:
                    diff = move_relative(
                        y_motor,
                        args.dy,
                        args.profile_vel,
                        args.profile_acc,
                        args.profile_dec,
                        args.timeout,
                    )
                    result["move_result"]["y_diff"] = diff

        result["after"] = {
            "x": dump_axis_state(client, "x", args.x_slave),
            "y": dump_axis_state(client, "y", args.y_slave),
        }

        text = json.dumps(result, ensure_ascii=False, indent=2)
        print(text)

        if args.output_json:
            out_path = Path(args.output_json)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
            print(f"\n已写出结果: {out_path}")


if __name__ == "__main__":
    main()
